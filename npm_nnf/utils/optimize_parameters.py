import numpy as np
import torch
import pandas as pd
import os
import pickle
from copy import deepcopy


from sklearn.model_selection import KFold, train_test_split, cross_val_score
from sklearn.model_selection import RandomizedSearchCV
from sklearn.model_selection import GridSearchCV

from scipy.stats import randint
from scipy.stats import uniform
from scipy.stats import loguniform

import importlib

import optuna
optuna.logging.set_verbosity(0)


import warnings
warnings.filterwarnings('ignore')

import sys
import matplotlib.pyplot as plt
plt.style.use('seaborn-whitegrid')
import logging
logging.basicConfig(level=logging.NOTSET,format='%(asctime)s  %(name)s %(levelname)s %(message)s')
logger = logging.getLogger('ROOT')
logger.setLevel(logging.INFO)



import npm_nnf.utils.utils_kernels as KT
import npm_nnf.density_estimation.utils_density as utils
import npm_nnf.density_estimation.utils_data as generators
import npm_nnf.utils.utils_train as utils_train

import sys, getopt
import json

torch.set_default_dtype(torch.float64)


def main(argv):
   n_jobs = None
   ds = None
   root_directory = ""
   try:
      opts, args = getopt.getopt(argv,"hc:r:nd",["config=","njobs=","dataset=","root-directory="])
   except getopt.GetoptError:
      print('optimize_parameters.py -c <config path>')
      sys.exit(2)

   for opt, arg in opts:
      if opt == '-h':
         print('optimize_parameters.py -c <config path>')
         sys.exit()
      if opt in ("-r","--root-directory"):
         root_directory=arg
      if opt in ("-c", "--config"):
         with open(arg) as config_file:
            data = json.load(config_file)

      if opt in ("-n","--njobs"):
         n_jobs = arg
      if opt in ("-d","--dataset"):
         ds = pickle.load(open(arg,'rb'))
   if isinstance(ds,type(None)):
      ds_path = os.path.join(root_directory,data['data_set_path'])
      ds = pickle.load(open(os.path.join(ds_path,data['data_set_file']),'rb'))

   d = ds.X.size(1)
   n = ds.X.size(0)
   if isinstance(n_jobs,type(None)):
      n_jobs = data["n_jobs"]
   version = data["version"]
   eta = data["eta"]
   cv = data["cv"]
   file_path = os.path.join(root_directory,data['save_path'])
   file_name = data['save_name']
   model_name = data['model'][1]
   file_name = f'{file_name}_{model_name}_dimension{d}_datasetsize{n}'
   if isinstance(version,type(None)):
      version = 0
      while os.path.isfile(os.path.join(file_path,f'{file_name}_{version+1}.pickle')):
         version += 1
      version += 1
   file_name = f'{file_name}_{version}.pickle'

   prune = data["prune"]
   n_trials = data["n_trials"]
   iii = importlib.import_module(data['model'][0])
   model = getattr(iii,model_name)
   fixed_params = data['fixed_parameters']
   if "mu_base" in fixed_params.keys():
      if isinstance(fixed_params['mu_base'],int):
         fixed_params['mu_base'] = torch.zeros((d,))
   variable_params = {}
   for key,value in data['variable_parameters'].items():
      variable_params[key] = [value["type"],value["min"],value["max"]]
   utils_train.perform_study(model, ds, fixed_params=fixed_params, variable_params=variable_params, cv=cv,
                                   prune=prune,
                                   n_trials=n_trials, file_path=file_path, file_name = file_name, eta=eta, n_jobs=n_jobs)



if __name__ == "__main__":
   main(sys.argv[1:])