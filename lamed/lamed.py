from __future__ import print_function
import sys
import hashlib
sys.path.insert(0, './vendor')
import redis
try:
    from lamed.config import config
    from lamed import logger
except ImportError:
    from config import config
    import logger

logger = logger.setup()

UUID_EXPIRY = config.get('uuid_expiry_seconds', 24 * 60 * 60)

def _redis():
    redis_config = config['redis']
    redis_config["charset"] = "utf-8"
    redis_config["decode_responses"] = True
    return redis.Redis(**redis_config)


def _counter_key(namespace, experiment, goal, variant):
    return '{0}:counters:{1}:{2}:{3}'.format(
        namespace,
        experiment,
        goal,
        variant)


def _results_dict(namespace, experiment):
    """ returns a dict in the following format:
        {namespace.counters.experiment.goal.variant: count}
    """
    r = _redis()
    keys = r.smembers("{0}:{1}:counter_keys".format(namespace, experiment))
    pipe = r.pipeline()
    for key in keys:
        pipe.get(key)
    values = pipe.execute()
    return dict(zip(keys, [int(value or 0) for value in values]))


def _experiment_goals(namespace, experiment):
    raw_results = _results_dict(namespace, experiment)
    variants = set([x.split(':')[-1] for x in raw_results.keys()])
    goals = set([x.split(':')[-2] for x in raw_results.keys()])
    goals.discard('participate')
    goal_results = []
    for goal in goals:
        goal_data = {'goal': goal, 'results': []}
        for variant in variants:
            trials = raw_results.get(
                _counter_key(namespace, experiment, 'participate', variant), 0)
            successes = raw_results.get(
                _counter_key(namespace, experiment, goal, variant), 0)
            goal_data['results'].append(
                {'label': variant,
                'successes': successes,
                'trials': trials})
        goal_results.append(goal_data)
    return goal_results


def _add_unique(pipe, key, uuid):
    logger.info("adding {} to {}".format(uuid, key))
    uuid = hashlib.sha1("{} {}".format(key, uuid).encode('utf-8')).hexdigest()
    logger.info("sha1 uuid = {}".format(uuid))
    while True:
        try:
            pipe.watch(uuid)
            uuid_exists = pipe.get(uuid)
            if uuid_exists is not None:
                logger.debug("{} exists".format(uuid))
                break
            pipe.multi()
            # setting a flag for the uuid with expiry time of UUID_EXPIRY
            pipe.setex(uuid, UUID_EXPIRY, "1")
            # incrementing counter for key
            pipe.incr(key)
            pipe.execute()
            logger.info("added {} to {}".format(uuid, key))
            break
        except redis.WatchError:
            logger.debug("watch error with {} {}".format(uuid, key))
            continue


def experiment(event, context):
    """ retrieves a single experiment results from redis
        params:
            - experiment - name of the experiment
            - namespace (optional)
    """
    experiment = event['experiment']
    namespace = event.get('namespace', 'alephbet')
    return _experiment_goals(namespace, experiment)


def all(event, context):
    """ retrieves all experiment results from redis
        params:
            - namespace (optional)
            - scope (optional, comma-separated list of experiments)
    """
    r = _redis()
    namespace = event.get('namespace', 'alephbet')
    scope = event.get('scope')
    if scope:
        experiments = scope.split(',')
    else:
        experiments = r.smembers("{0}:experiments".format(namespace))
    results = []
    results.append({'meta': {'scope': scope}})
    for ex in experiments:
        goals = experiment({'experiment': ex, 'namespace': namespace}, context)
        results.append({'experiment': ex, 'goals': goals})
    return results


def track(event, context):
    """ tracks an alephbet event (participate, goal etc)
        params:
            - experiment - name of the experiment
            - uuid - a unique id for the event
            - variant - the name of the variant
            - event - either the goal name or 'participate'
            - namespace (optional)
    """
    experiment = event['experiment']
    namespace = event.get('namespace', 'alephbet')
    uuid = event['uuid']
    variant = event['variant']
    tracking_event = event['event']

    r = _redis()
    key = '{0}:counters:{1}:{2}:{3}'.format(
        namespace, experiment, tracking_event, variant)
    with r.pipeline() as pipe:
        pipe.sadd('{0}:experiments'.format(namespace), experiment)
        pipe.sadd('{0}:counter_keys'.format(namespace), key)
        pipe.sadd('{0}:{1}:counter_keys'.format(namespace, experiment), key)
        pipe.execute()
        _add_unique(pipe, key, uuid)


def delete(event, context):
    """ delete an experiment
        params:
            - experiment - name of the experiment
            - namespace
    """

    r = _redis()
    namespace = event.get('namespace', 'alephbet')
    experiment = event['experiment']
    experiments_set_key = '{0}:experiments'.format(namespace)
    experiment_counters_set_key = '{0}:{1}:counter_keys'.format(namespace, experiment)
    all_counters_set_key = '{0}:counter_keys'.format(namespace)

    if r.sismember(experiments_set_key, experiment):
        counter_keys = r.smembers(
            experiment_counters_set_key
        )
        pipe = r.pipeline()
        for key in counter_keys:
            pipe.srem(all_counters_set_key, key)
            pipe.delete(key)
        pipe.delete(
            experiment_counters_set_key
        )
        pipe.srem(
            experiments_set_key,
            experiment
        )
        pipe.execute()
