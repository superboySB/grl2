from typing import Tuple, Dict
import tensorflow as tf

from core.elements.model import Model
from core.mixin.actor import RMS
from core.typing import ModelPath
from utility.utils import set_path
from utility.tf_utils import numpy2tensor, tensor2numpy
from utility.typing import AttrDict


class Actor:
    def __init__(
        self, 
        *, 
        config: AttrDict, 
        model: Model, 
        name: str
    ):
        self._raw_name = name
        self._name = f'{name}_actor'
        if config.get('root_dir'):
            self._model_path = ModelPath(config.root_dir, config.model_name)
        else:
            self._model_path = None
        self.config = config

        self.rms: RMS = self.config.get('rms', None)
        self.setup_checkpoint()

        self.model = model
        
        self._post_init()

    @property
    def name(self):
        return self._name

    def _post_init(self):
        pass

    def reset_model_path(self, model_path: ModelPath):
        self._model_path = model_path
        self.config = set_path(self.config, model_path, max_layer=0)
        self.setup_checkpoint()
        if self.rms is not None:
            self.rms.reset_path(model_path)

    def __getattr__(self, name):
        # Do not expose the interface of independent elements here. 
        # Invoke them directly instead
        if name.startswith('_'):
            raise AttributeError("attempted to get missing private attribute '{}'".format(name))
        elif hasattr(self.rms, name):
            return getattr(self.rms, name)
        else:
            raise AttributeError(f"no attribute '{name}' is found")

    def __call__(
        self, 
        inp: dict,
        evaluation: bool=False, 
        return_eval_stats: bool=False
    ):
        """ The interface to interact with the environment
        
        Args:
            inp: input to the calling method
            evaluation: evaluation mode or not
            return_eval_stats: if return evaluation stats
        Return:
            (action, terms, rnn_state)
        """
        inp, tf_inp = self._process_input(inp, evaluation)
        tf_inp = self._add_eval(
            tf_inp, 
            evaluation=evaluation, 
            return_eval_stats=return_eval_stats
        )
        
        out = self.model.action(**tf_inp)
        out = self._process_output(inp, out, evaluation)

        return out

    """ Overwrite the following methods if necessary """
    def _process_input(
        self, 
        inp: dict, 
        evaluation: bool
    ):
        """ Processes input to Model at the algorithmic level 
        
        Args:
            inp: input to the model
            evaluation bool: evaluation mode or not
        Returns: 
            processed input to <model.action>
        """
        if self.rms is not None:
            inp = self.rms.process_obs_with_rms(
                inp, mask=inp.get('life_mask'), 
                update_rms=self.config.get('update_obs_rms_at_execution', False)
            )
        return inp, numpy2tensor(inp)

    def _add_eval(
        self, 
        tf_inp, 
        evaluation, 
        return_eval_stats
    ):
        if not self.model.to_build:
            tf_inp.update({
                'evaluation': evaluation,
                'return_eval_stats': return_eval_stats
            })
        return tf_inp

    def _process_output(
        self, 
        inp: dict, 
        out: Tuple[tf.Tensor, Dict[str, tf.Tensor]], 
        evaluation: bool
    ):
        """ Post-processes output. By default, 
        we convert tf.Tensor to np.ndarray
        
        Args:
            inp: Pre-processed inputs
            out: Model output
        Returns:
            (action, terms, rnn_state)
        """
        action, terms, state = out
        if state is not None:
            action, terms, prev_state = tensor2numpy(
                (action, terms, inp['state']))
            if not evaluation:
                terms.update({
                    'mask': inp['mask'], 
                    **prev_state._asdict(),
                })
        else:
            action, terms = tensor2numpy((action, terms))
        if self.config.get('update_obs_at_execution', True) \
            and not evaluation and self.rms is not None \
                and self.rms.is_obs_normalized:
            terms.update({k: inp[k] 
                for k in self.config.rms.obs_names})
        return action, terms, state

    def normalize_reward(self, reward):
        if self.rms is not None:
            return self.rms.normalize_reward(reward)
        return reward

    def get_weights(self, identifier=None):
        if identifier is None:
            identifier = self._raw_name
        weights = {
            f'{identifier}_model': self.model.get_weights(),
            f'{identifier}_aux': self.get_auxiliary_stats()
        }
        return weights

    def set_weights(self, weights, identifier=None):
        if identifier is None:
            identifier = self._raw_name
        self.model.set_weights(weights[f'{identifier}_model'])
        if f'{identifier}_aux' in weights:
            self.set_auxiliary_stats(weights[f'{identifier}_aux'])

    def get_model_weights(self, name: str=None):
        return self.model.get_weights(name)

    def set_model_weights(self, weights):
        self.model.set_weights(weights)

    def get_auxiliary_stats(self):
        if self.rms:
            return self.rms.get_rms_stats()

    def set_auxiliary_stats(self, rms_stats):
        if self.rms:
            self.rms.set_rms_stats(rms_stats)

    def save_auxiliary_stats(self):
        """ Save the RMS and the model """
        if self.rms:
            self.rms.save_rms()

    def restore_auxiliary_stats(self):
        """ Restore the RMS and the model """
        if self.rms:
            self.rms.restore_rms()

    def setup_checkpoint(self):
        if self.rms:
            self.config.rms.model_path = self._model_path
            self.rms = RMS(self.config.rms)

    def save(self):
        self.model.save()
        self.save_auxiliary_stats()
    
    def restore(self):
        self.model.restore()
        self.restore_auxiliary_stats()


def create_actor(config, model, name='actor'):
    return Actor(config=config, model=model, name=name)
