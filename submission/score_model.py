import datetime
import json
import logging
import os
import subprocess
import sys
import zipfile
import git
from pathlib import Path

from importlib import import_module

from brainscore.utils import LazyLoad

from submission import score_model
from submission.ml_pool import MLBrainPool, ModelLayers

logger = logging.getLogger(__name__)

all_benchmarks_list = [
    'movshon.FreemanZiemba2013.V1-pls',
    'movshon.FreemanZiemba2013.V2-pls',
    'dicarlo.Majaj2015.V4-pls', 'dicarlo.Majaj2015.IT-pls',
    'dicarlo.Rajalingham2018-i2n',
    # 'dicarlo.Kar2019-ost',
    'fei-fei.Deng2009-top1'
]


def score_models(config_file, work_dir, db_connection_config, jenkins_id, models=None,
                 benchmarks=None):
    config_file = config_file if os.path.isfile(config_file) else os.path.realpath(config_file)
    db_conn = connect_db(db_connection_config)
    with open(config_file) as file:
        configs = json.load(file)
    print(configs)
    if configs['type'] == 'zip':
        config_path = Path(config_file).parent
        logger.info('Start executing models in repo %s' % ( configs['zip_filename']))
        repo = extract_zip_file(configs, config_path, work_dir)
    else:
        logger.info('Start executing models in repo %s' % (configs['git_url']))
        repo = clone_repo(configs, work_dir)
    package = 'models.brain_models' if configs['model_type'] is 'BrainModel' else 'models.base_models'
    module = install_project(repo, package)
    test_benchmarks = all_benchmarks_list if benchmarks is None or len(benchmarks) == 0 else benchmarks
    ml_brain_pool = {}
    if configs['model_type'] == 'BaseModel':
        test_models = module.get_model_list() if models is None or len(models) == 0 else models
        logger.info(f"Start working with base models")
        layers = {}
        base_model_pool = {}
        for model in test_models:
            function = lambda: module.get_model(model)
            base_model_pool[model] = LazyLoad(function)
            if module.get_layers is not None:
                layers[model] = module.get_layers(model)
        model_layers = ModelLayers(layers)
        ml_brain_pool = MLBrainPool(base_model_pool, model_layers)
    else:
        logger.info(f"Start working with brain models")
        test_models = module.get_model_list() if models is None or len(benchmarks) == 0 else models
        for model in test_models:
            ml_brain_pool[model] = module.get_model(model)
    file = open(f'result_{jenkins_id}.txt', 'w')

    file.write(f'Executed following benchmarks: {test_benchmarks}\n')
    file.write('Model|Benchmark|raw result|ceiled result|error|finished time \n')
    try:
        for model in test_models:
            scores = []
            for benchmark in test_benchmarks:
                try:
                    logger.info(f"Scoring {model} on benchmark {benchmark}")
                    score = score_model(model, benchmark, ml_brain_pool[model])
                    scores.append(score.sel(aggregation='center').values)
                    logger.info(f'Running benchmark {benchmark} on model {model} produced this score: {score}')
                    if benchmark == 'fei-fei.Deng2009-top1':
                        raw = score.sel(aggregation='center').item(0)
                        ceiled = raw
                        error = 0
                    else:
                        raw = score.raw.sel(aggregation='center').item(0)
                        ceiled = score.sel(aggregation='center').item(0)
                        error = score.sel(aggregation='error').item(0)
                    finished = datetime.datetime.now()
                    store_score(db_conn, (model,
                                          benchmark,
                                          raw, ceiled, error,
                                          finished,
                                          jenkins_id,
                                          configs['email'],
                                          configs['name']))
                    file.write(f'{model}|{benchmark}|{raw}|{ceiled}|{error}|{finished}\n')
                except Exception as e:
                    logging.error(f'Could not run model {model} because of following error')
                    logging.error(e, exc_info=True)
                    file.write(f'{model}|{benchmark}|Execution error: {str(e)}\n')
    finally:
        file.close()
        db_conn.close()


def connect_db(db):
    with open(db) as file:
        db_configs = json.load(file)
    import psycopg2
    return psycopg2.connect(host=db_configs['hostname'], user=db_configs['user_name'], password=db_configs['password'],
                            dbname=db_configs['database'])


def store_score(dbConnection, score):
    insert = '''insert into benchmarks_score
            (model, benchmark, score_raw, score_ceiled, error, timestamp, jenkins_job_id, user_id, name)   
            VALUES(%s,%s,%s,%s,%s,%s, %s, %s, %s)'''
    logging.info(f'Run results{score}')
    cur = dbConnection.cursor()
    cur.execute(insert, score)
    dbConnection.commit()
    return


def extract_zip_file(config, config_path, work_dir):
    zip_file = '%s/%s' % (config_path, config['zip_filename'])
    zip_file = zip_file if os.path.isfile(zip_file) else os.path.realpath(zip_file)
    with zipfile.ZipFile(zip_file, 'r') as model_repo:
        model_repo.extractall(path=work_dir)
    #     Use the single directory in the zip file
    path = '%s/%s' % (work_dir, os.listdir(work_dir)[0])
    path = path if os.path.isfile(path) else os.path.realpath(path)
    return path


def clone_repo(config, work_dir):
    git.Git(work_dir).clone(config['git_url'])
    return '%s/%s' % (work_dir, os.listdir(work_dir)[0])


def install_project(repo, package):
    try:
        print(os.environ["PYTHONPATH"])
        subprocess.call([sys.executable, "-m", "pip", "install", repo], env=os.environ)
        sys.path.insert(1, repo)
        print(sys.path)
        return import_module(package)
    except ImportError:
        return __import__(package)
