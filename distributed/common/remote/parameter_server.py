import collections
import itertools
import os
import filelock
import random
import time
from typing import Dict, List
import numpy as np
import jax
import ray

from core.ckpt import pickle
from core.elements.builder import ElementsBuilderVC
from core.log import do_logging
from core.names import PATH_SPLIT
from core.mixin.actor import RMSStats, combine_rms_stats, rms2dict
from core.names import *
from core.remote.base import RayBase
from core.typing import AttrDict, AttrDict2dict, ModelPath, ModelWeights, \
  construct_model_name, exclude_subdict, get_aid, get_date, get_basic_model_name
from nn.utils import reset_linear_weights, reset_weights
from rule.utils import is_rule_strategy
from run.utils import search_for_config
from tools.schedule import PiecewiseSchedule
from tools.timer import Every
from tools.utils import config_attr, dict2AttrDict
from tools import yaml_op
from distributed.common.typing import Status
from distributed.common.remote.payoff import PayoffManager


""" Name Conventions:

We use "model" and "strategy" interchangeably. 
In general, we prefer the term "strategy" in the context of 
training and inference, and the term "model" when a model 
path is involved (e.g., when saving&restoring a model).
"""


class SCORE_METRICS:
  SCORE = 'score'
  WIN_RATE = 'win_rate'


def _divide_runners(n_agents, n_runners, online_frac):
  if n_runners < n_agents:
    return n_runners, 0
  n_agent_runners = int(n_runners * (1 - online_frac) // n_agents)
  n_online_runners = n_runners - n_agent_runners * n_agents
  assert n_agent_runners * n_agents + n_online_runners == n_runners, \
    (n_agent_runners, n_agents, n_online_runners, n_runners)
  return n_online_runners, n_agent_runners


def _reset_policy_head(weights, config):
  rng = jax.random.PRNGKey(random.randint(0, 2**32))
  if 'policies' in weights[MODEL]:
    for policy in weights[MODEL]['policies']:
      for k, v in policy.items():
        if k.startswith('policy/head'):
          policy[k] = reset_linear_weights(
            v, rng, 'orthogonal', scale=.01)
          do_logging(f'{k} is reset', color='green')
        elif k == 'policy':
          for kk, vv in policy[k].items():
            if kk.endswith('logstd'):
              policy[k][kk] = reset_weights(
                vv, rng, 'constant', value=config.model.policy.init_std)
  elif 'policy' in weights[MODEL]:
    policy = weights[MODEL]['policy']
    for k, v in policy.items():
      if k.startswith('policy/head'):
        policy[k] = reset_linear_weights(
          v, rng, 'orthogonal', scale=.01)
        do_logging(f'{k} is reset', color='green')
      elif k == 'policy':
        for kk, vv in policy[k].items():
          if kk.endswith('logstd'):
            policy[k][kk] = reset_weights(
              vv, rng, 'constant', value=config.model.policy.init_std)
  return weights


class ParameterServer(RayBase):
  def __init__(
    self, 
    config: dict,  
    to_restore_params=True, 
    name='parameter_server',
  ):
    super().__init__(seed=config.get('seed'))
    config = dict2AttrDict(config)
    self.config = config.parameter_server
    self.score_metrics = self.config.get('score_metrics', SCORE_METRICS.SCORE)
    assert self.score_metrics in (SCORE_METRICS.SCORE, SCORE_METRICS.WIN_RATE)
    self.name = name

    self.n_agents = config.n_agents
    self.n_runners = config.runner.n_runners

    # the probability of training an agent from scratch
    self.train_from_scratch_frac = self.config.get('train_from_scratch_frac', 1)
    self.train_from_latest_frac = self.config.get('train_from_latest_frac', 0)
    self.hist_train_scale = self.config.get('hist_train_scale', 1)
    self.reset_policy_head_frac = self.config.get('reset_policy_head_frac', 1)
    # fraction of runners devoted to the play of the most recent strategies 
    self.online_frac = self.config.get('online_frac', .2)
    self.online_scheduler = PiecewiseSchedule(self.online_frac, interpolation='stage')
    self.self_play = self.config.get('self_play', False)
    assert not self.self_play, self.self_play

    model_name = get_basic_model_name(config.model_name)
    self._dir = os.path.join(config.root_dir, model_name)
    os.makedirs(self._dir, exist_ok=True)
    self._path = os.path.join(self._dir, f'{self.name}.yaml')

    self._pool_name = self.config.get('pool_name', 'strategy_pool')
    date = get_date(config.model_name)
    self._pool_path = os.path.join(config.root_dir, f'{date}', f'{self._pool_name}.yaml')

    self._params: List[Dict[ModelPath, Dict]] = [{} for _ in range(self.n_agents)]
    self._prepared_strategies: List[List[ModelWeights]] = \
      [[None for _ in range(self.n_agents)] for _ in range(self.n_runners)]
    self._reset_ready()

    self._rule_strategies = set()

    self._opp_dist: Dict[ModelPath, List[float]] = {}
    self._to_update: Dict[ModelPath, Every] = collections.defaultdict(
      lambda: Every(self.config.setdefault('update_interval', 1), -1))

    self._former_models: List[ModelPath] = [None for _ in range(self.n_agents)]
    # an active model is the one under training
    self._active_models: List[ModelPath] = [None for _ in range(self.n_agents)]
    # is the first pbt iteration
    self._iteration = 1
    self._all_strategies = None

    self.payoff_manager: PayoffManager = PayoffManager(
      self.config.payoff, self.n_agents, self._dir, self_play=self.self_play)

    succ = self.restore(to_restore_params)
    self._update_runner_distribution()

    if self.config.get('rule_strategies'):
      self.add_rule_strategies(self.config.rule_strategies, local=succ)

    self.check()

  def _reset_ready(self):
    self._ready = [False for _ in range(self.n_runners)]

  def check(self):
    assert self.payoff_manager.size() == len(self._params[0]), (self.payoff_manager.size(), len(self._params))

  def build(self, configs: List[Dict], env_stats: Dict):
    self.configs = [dict2AttrDict(c) for c in configs]
    self.builders: List[ElementsBuilderVC] = []
    for aid, config in enumerate(configs):
      model = os.path.join(config["root_dir"], config["model_name"])
      assert model.rsplit(PATH_SPLIT)[-1] == f'a{aid}', model
      os.makedirs(model, exist_ok=True)
      builder = ElementsBuilderVC(config, env_stats, to_save_code=False)
      self.builders.append(builder)
    assert len(self.builders) == self.n_agents, (len(configs), len(self.builders), self.n_agents)

  """ Strategy Pool Management """
  def save_strategy_pool(self):
    lock = filelock.FileLock(self._pool_path + '.lock')

    while True:
      with lock.acquire(timeout=5):
        config = yaml_op.load(self._pool_path)
        config[self._dir] = [[list(m) for m in v] for v in self._params]
        yaml_op.dump(self._pool_path, **config)
        break
      do_logging(f'{self._pool_path} is blocked for 5s', 'red')
    do_logging(f'Saving strategy pool to {self._pool_path}', color='green')

  def load_strategy_pool(self):
    lock = filelock.FileLock(self._pool_path + '.lock')

    while True:
      with lock.acquire(timeout=5):
        config = yaml_op.load(self._pool_path)
        break
      do_logging(f'{self._pool_path} is blocked for 5s', 'red')

    for v in config.values():
      for aid, models in enumerate(v):
        for model in models:
          model = ModelPath(*model)
          if model not in self._params[aid]:
            self.restore_params(model)
            self.add_strategy_to_payoff(model, aid=aid)
    self.check()
    do_logging(f'Loading strategy pool from {self._pool_path}', color='green')

  """ Data Retrieval """
  def get_configs(self):
    return self.configs

  def get_active_models(self):
    return self._active_models

  def get_active_aux_stats(self):
    active_stats = {m: self.get_aux_stats(m) for m in self._active_models}

    return active_stats

  def get_aux_stats(self, model_path: ModelPath):
    aid = get_aid(model_path.model_name)
    rms = self._params[aid][model_path].get(ANCILLARY, RMSStats({}, None))
    stats = rms2dict(rms)

    return stats

  def get_opponent_distributions_for_active_models(self):
    dists = {
      m: self.payoff_manager.compute_opponent_distribution(i, m, False) 
      for i, m in enumerate(self._active_models)
    }
    for m, (p, d) in dists.items():
      for x in d:
        if x.size > 1:
          online_frac = self.online_scheduler(self._iteration)
          x /= np.nansum(x[:-1]) / (1 - online_frac)
          x[-1] = online_frac
      dists[m] = (p, d)

    return dists

  def get_runner_stats(self):
    self._update_runner_distribution()
    if self._iteration == 1 and not self._rule_strategies:
      stats = AttrDict(
        iteration=self._iteration, 
        online_frac=1,
        n_online_runners=self.n_online_runners, 
        n_agent_runners=self.n_agent_runners, 
      )
    else:
      stats = AttrDict(
        iteration=self._iteration, 
        online_frac=self.online_scheduler(self._iteration), 
        n_online_runners=self.n_online_runners, 
        n_agent_runners=self.n_agent_runners, 
      )

    return stats

  """ Strategy Management """
  def add_rule_strategies(self, rule_config: dict, local=False):
    models = []
    for name, config in rule_config.items():
      aid = config['aid']
      assert aid < self.n_agents, (aid, self.n_agents)
      vid = config['vid']
      model_name = get_basic_model_name(self.config.model_name)
      model_name = os.path.join(model_name, f'{name}-rule')
      model_name = construct_model_name(model_name, aid, vid, vid)
      model = ModelPath(self.config.root_dir, model_name)
      self._rule_strategies.add(model)
      self._params[aid][model] = AttrDict2dict(config)
      models.append(model)
      do_logging(f'Adding rule strategy: {model}', color='green')
      if not local:
        # Add the rule strategy to the payoff table if the payoff manager is not restored from a checkpoint
        self.add_strategy_to_payoff(model, aid=aid)

  def add_strategy_to_payoff(self, model: ModelPath, aid: int):
    self.payoff_manager.add_strategy(model, aid=aid)

  def add_strategies_to_payoff(self, models: List[ModelPath]):
    assert len(models) == self.n_agents, models
    self.payoff_manager.add_strategies(models)

  def _update_active_model(self, aid, model: ModelPath):
    self._active_models[aid] = model

  def _update_active_models(self, models: List[ModelPath]):
    assert len(models) == self.n_agents, models
    self._active_models = models

  def _reset_prepared_strategy(self, rid: int=-1):
    raise NotImplementedError

  def get_prepared_strategies(self, rid: int=-1):
    if rid < 0:
      if not all(self._ready):
        return None
      strategies = self._prepared_strategies
    else:
      if not self._ready[rid]:
        return None
      strategies = self._prepared_strategies[rid]
    self._reset_prepared_strategy(rid)
    return strategies

  def update_and_prepare_strategy(
    self, 
    aid: int, 
    model_weights: ModelWeights, 
    step=None
  ):
    def put_model_weights(aid, mid, models):
      mids = []
      for i, m in enumerate(models):
        if i == aid:
          # We directly store mid for the agent with aid
          mids.append(mid)
        else:
          if m in self._rule_strategies:
            # rule-based strategy
            weights = self._params[i][m]
          else:
            # if error happens here
            # it's likely that you retrive the latest model 
            # in self.payoff_manager.sample_opponent_strategies
            weights = {
              k: self._params[i][m][k] 
              for k in [MODEL, 'train_step', ANCILLARY]
              if k in self._params[i][m]
            }
          mids.append(ray.put(ModelWeights(m, weights)))
      return mids

    def get_historical_mids(aid, mid, model_weights: ModelWeights):
      model = model_weights.model
      assert aid == get_aid(model.model_name), (aid, model)
      models = self.sample_opponent_strategies(aid, model, step)
      assert model in models, (model, models)
      assert len(models) == self.n_agents, (self.n_agents, models)
      mids = put_model_weights(aid, mid, models)
      assert len(mids) == self.n_agents, (self.n_agents, mids)

      return mids
    
    def prepare_recent_models(aid, mid, n_runners):
       # prepare the most recent model for the first n_runners runners
      for rid in range(n_runners):
        self._prepared_strategies[rid][aid] = mid
        self._ready[rid] = all(
          [m is not None for m in self._prepared_strategies[rid]]
        )

    def prepare_historical_models(aid, mid, model_weights: ModelWeights):
      rid_min = self.n_online_runners + aid * self.n_agent_runners
      rid_max = self.n_online_runners + (aid + 1) * self.n_agent_runners
      mids = get_historical_mids(aid, mid, model_weights)
      for rid in range(rid_min, rid_max):
        self._prepared_strategies[rid] = mids
        self._ready[rid] = True

    def prepare_models(aid, model_weights: ModelWeights):
      model_weights.weights.pop(OPTIMIZER)
      model_weights.weights[ANCILLARY] = \
        self._params[aid][model_weights.model].get(ANCILLARY, RMSStats({}, None))
      mid = ray.put(model_weights)

      # prepare the most recent model for online runners
      prepare_recent_models(aid, mid, self.n_online_runners)
      if self.n_online_runners < self.n_runners:
        # prepare historical models for selected runners
        prepare_historical_models(aid, mid, model_weights)

    assert self._active_models[aid] == model_weights.model, (self._active_models, model_weights.model)
    assert set(model_weights.weights) == set([MODEL, OPTIMIZER, 'train_step']), list(model_weights.weights)
    assert aid == get_aid(model_weights.model.model_name), (aid, model_weights.model)
    
    self._params[aid][model_weights.model].update(model_weights.weights)
    model_weights = ModelWeights(model_weights.model, model_weights.weights.copy())
    prepare_models(aid, model_weights)
    # do_logging(f'Receiving weights of train step {model_weights.weights["train_step"]}')

  def update_aux_stats(self, aid, model_weights: ModelWeights):
    assert len(model_weights.weights) == 1, list(model_weights.weights)
    assert ANCILLARY in model_weights.weights, list(model_weights.weights)
    assert aid == get_aid(model_weights.model.model_name), (aid, model_weights.model)
    if self._params[aid][model_weights.model] is not None \
        and ANCILLARY in self._params[aid][model_weights.model]:
      self._params[aid][model_weights.model][ANCILLARY] = combine_rms_stats(
        self._params[aid][model_weights.model][ANCILLARY], 
        model_weights.weights[ANCILLARY],
      )
    else:
      self._params[aid][model_weights.model][ANCILLARY] = model_weights.weights[ANCILLARY]

  def _update_runner_distribution(self):
    if self._iteration == 1 and not self._rule_strategies:
      self.n_online_runners = self.n_runners
      self.n_agent_runners = 0
    else:
      online_frac = self.online_scheduler(self._iteration)
      self.n_online_runners, self.n_agent_runners = _divide_runners(
        self.n_agents, self.n_runners, online_frac
      )

  def _restore_active_strategies(self):
    # restore active strategies 
    strategies = []
    for aid, model in enumerate(self._active_models):
      assert aid == get_aid(model.model_name), f'Inconsistent aids: {aid} vs {get_aid(model.model_name)}({model})'
      weights = self._params[aid][model].copy()
      weights.pop(ANCILLARY, None)
      strategies.append(ModelWeights(model, weights))
      do_logging(f'Restoring active strategy: {model}', color='green')
    for b in self.builders:
      config = b.config.copy(shallow=False)
      config.status = Status.TRAINING
      b.save_config(config)
    return strategies

  def _construct_raw_strategy(self, aid, iteration):
    self.builders[aid].set_iteration(iteration)
    config = self.builders[aid].config.copy(shallow=False)
    config.status = Status.TRAINING
    self.builders[aid].save_config(config)
    model = self.builders[aid].get_model_path()
    assert aid == get_aid(model.model_name), f'Inconsistent aids: {aid} vs {get_aid(model.model_name)}({model})'
    assert model not in self._params[aid], (model, list(self._params[aid]))
    self._params[aid][model] = {}
    weights = None
    model_weights = ModelWeights(model, weights)
    do_logging(f'Sampling raw strategy for training: {model}', color='green')
    
    return model_weights

  def _sample_with_prioritization(self, aid: int):
    candidates = []
    candidate_scores = []
    candidate_weights = []
    for m in self._params:
      if not is_rule_strategy(m):
        candidates.append(m)
        score = self.get_avg_score(aid, m)
        candidate_scores.append(score)
    candidate_scores = np.array(candidate_scores)
    candidate_scores = candidate_scores - np.min(candidate_scores) + .01 # avoid zero weights
    candidate_weights = candidate_scores ** self.hist_train_scale
    idx = random.choices(range(len(candidates)), candidate_weights)[0]
    model = candidates[idx]
    model_scores = [f'{m}={s}' for m, s in zip(self._params, candidate_scores)]
    do_logging(f'Sampling historical stratgy({model}={candidate_scores[idx]})'
                f' from {model_scores}', color='green')
    return model

  def _sample_historical_strategy(self, aid, iteration):
    if self._former_models[aid] and random.random() < self.train_from_latest_frac:
      model = self._former_models[aid]
      do_logging(f'Sampling historical stratgy({model}) from {list(self._params)}', color='green')
    else:
      model = self._sample_with_prioritization()
    assert aid == get_aid(model.model_name), f'Inconsistent aids: {aid} vs {get_aid(model.model_name)}({model})'
    weights = self._params[aid][model].copy()
    weights.pop(ANCILLARY)
    config = search_for_config(model)
    model = self.builders[aid].get_sub_version(config, iteration)
    config = self.builders[aid].config.copy(shallow=False)
    config.status = Status.TRAINING
    self.builders[aid].save_config(config)
    assert aid == get_aid(model.model_name), f'Inconsistent aids: {aid} vs {get_aid(model.model_name)}({model})'
    assert model not in self._params[aid], f'{model} is already in {list(self._params[aid])}'
    if random.random() < self.reset_policy_head_frac:
      weights = _reset_policy_head(weights, config)
    self._params[aid][model] = weights
    model_weights = ModelWeights(model, weights)
    
    return model_weights

  def sample_training_strategies(self, iteration=None):
    if iteration is not None:
      assert iteration == self._iteration, (iteration, self._iteration)
    strategies = []
    is_raw_strategy = [False for _ in range(self.n_agents)]
    if any([am is not None for am in self._active_models]):
      strategies = self._restore_active_strategies()
    else:
      assert all([am is None for am in self._active_models]), self._active_models
      for aid in range(self.n_agents):
        if self._iteration == 1 or random.random() < self.train_from_scratch_frac:
          model_weights = self._construct_raw_strategy(aid, self._iteration)
          is_raw_strategy[aid] = True
        else:
          model_weights = self._sample_historical_strategy(aid, self._iteration)
        strategies.append(model_weights)
      models = [s.model for s in strategies]
      self.add_strategies_to_payoff(models)
      self._update_active_models(models)
      self.save_active_models(0, 0)
      self.save_strategy_pool()
      self.save()

    return strategies, is_raw_strategy

  def archive_training_strategies(self, **kwargs):
    do_logging('Archiving training strategies', color='green')
    for b in self.builders:
      config = b.config.copy(shallow=False)
      config.update(kwargs)
      b.save_config(config)
    self._former_models = self._active_models.copy()
    for aid, model in enumerate(self._active_models):
      self.save_params(model)
      self._update_active_model(aid, None)
      if model in self._opp_dist:
        del self._opp_dist[model]
    self._iteration += 1
    self._reset_ready()
    self._update_runner_distribution()
    self.save()

  """ Strategy Sampling """
  def sample_opponent_strategies(self, aid, model: ModelPath, step=None):
    assert model == self._active_models[aid], (model, self._active_models)
    self._update_opp_distributions(aid, model)
    strategies = self.sample_strategies_with_opp_dists(
      aid, model, self._opp_dist[model]
    )
    assert model in strategies, (model, strategies)

    return strategies
  
  def sample_strategies_with_opp_dists(
    self, aid, model: ModelPath, opp_dists: List[np.ndarray],
  ):
    sid2model = self.payoff_manager.get_sid2model()
    models = [
      (random.choices(
        sid2model[i][:-1], 
        weights=opp_dists[i if i < aid else i-1][:-1]
      )[0] if len(sid2model[i]) > 1 else sid2model[i][0])
      if i != aid else model
      for i in range(self.n_agents)
    ]
    return models

  def sample_strategies_for_evaluation(self):
    if self._all_strategies is None:
      strategies = self.payoff_manager.get_all_strategies()
      self._all_strategies = [mw for mw in itertools.product(
        *[[s for s in ss] for ss in strategies])]
      assert len(self._all_strategies) == np.product([len(s) for s in strategies]), \
        (len(self._all_strategies), np.product([len(s) for s in strategies]))

    return self._all_strategies

  """ Payoff Operations """
  def get_avg_score(self, aid: int, model: ModelPath):
    payoffs = self.get_payoffs_for_model(aid, model)
    n_valid_payoffs = np.sum(~np.isnan(payoffs))
    if n_valid_payoffs > payoffs.size / 2:
      return np.nanmean(payoffs)
    else:
      if self.score_metrics == SCORE_METRICS.SCORE:
        return -1
      else:
        return 0

  def reset_payoffs(self, from_scratch=True, name=None):
    self.payoff_manager.reset(from_scratch=from_scratch, name=name)

  def get_payoffs(self, fill_nan=False):
    return self.payoff_manager.get_payoffs(fill_nan=fill_nan)

  def get_payoffs_for_model(self, aid: int, model: ModelPath):
    return self.payoff_manager.get_payoffs_for_model(aid, model)

  def get_counts(self):
    return self.payoff_manager.get_counts()

  def update_payoffs(self, models: List[ModelPath], scores: List[List[float]]):
    self.payoff_manager.update_payoffs(models, scores)
    self.payoff_manager.save(to_print=False)

  def _update_opp_distributions(self, aid, model: ModelPath):
    assert isinstance(model, ModelPath), model
    payoffs, self._opp_dist[model] = self.payoff_manager.\
      compute_opponent_distribution(aid, model)
    do_logging(f'Updating opponent distributions for agent {aid}: {self._opp_dist[model]} with payoffs {payoffs}', color='green')

  """ Checkpoints """
  def save_active_model(self, model: ModelPath, train_step: int, env_step: int):
    do_logging(f'Saving active model: {model}', color='green')
    assert model in self._active_models, (model, self._active_models)
    aid = get_aid(model.model_name)
    assert model in self._params[aid], f'{model} does not in {list(self._params[aid])}'
    self._params[aid][model]['train_step'] = train_step
    self._params[aid][model]['env_step'] = env_step
    self.save_params(model)

  def save_active_models(self, train_step: int, env_step: int):
    for m in self._active_models:
      self.save_active_model(m, train_step, env_step)

  def save_params(self, model: ModelPath, name='params'):
    assert model in self._active_models, (model, self._active_models)
    aid = get_aid(model.model_name)
    if MODEL in self._params[aid][model]:
      pickle.save_params(
        self._params[aid][model][MODEL], model, f'{name}/model')
    if OPTIMIZER in self._params[aid][model]:
      pickle.save_params(
        self._params[aid][model][OPTIMIZER], model, f'{name}/opt')
    rest_params = exclude_subdict(self._params[aid][model], MODEL, OPTIMIZER)
    if rest_params:
      pickle.save_params(rest_params, model, name)

  def restore_params(self, model: ModelPath, name='params'):
    aid = get_aid(model.model_name)
    params = pickle.restore_params(model, name)
    self._params[aid][model] = params

  def save(self):
    self.payoff_manager.save()
    model_paths = [[list(mn) for mn in p] for p in self._params]
    active_models = [list(m) if m is not None else m for m in self._active_models]
    yaml_op.dump(
      self._path, 
      model_paths=model_paths, 
      active_models=active_models, 
      iteration=self._iteration, 
      n_online_runners=self.n_online_runners, 
      n_agent_runners=self.n_agent_runners
    )

  def restore(self, to_restore_params=True):
    self.payoff_manager.restore()
    if os.path.exists(self._path):
      config = yaml_op.load(self._path)
      if config is None:
        return
      model_paths = config.pop('model_paths')
      for aid, model in enumerate(config.pop('active_models')):
        if model is not None:
          model = ModelPath(*model)
        self._update_active_model(aid, model)
      config_attr(self, config, config_as_attr=False, private_attr=True)
      if to_restore_params:
        for models in model_paths:
          for m in models:
            m = ModelPath(*m)
            if not is_rule_strategy(m):
              self.restore_params(m)
      return True
    else:
      return False


if __name__ == '__main__':
  from env.func import get_env_stats
  from tools.yaml_op import load_config
  config = load_config('algo/gd/configs/builtin.yaml')
  env_stats = get_env_stats(config['env'])
  ps = ParameterServer(config, env_stats)
