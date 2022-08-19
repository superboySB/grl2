"""Base class for an agent that defines the possible actions. """

from gym.spaces import Box
from gym.spaces import Discrete
import numpy as np
#import utils.utility_funcs as util

# basic moves every agent should do
AGENT_ACTIONS = {0: 'MOVE_LEFT',  # Move left
                1: 'MOVE_RIGHT',  # Move right
                2: 'MOVE_UP',  # Move up
                3: 'MOVE_DOWN',  # Move down
                4: 'STAY'  # don't move
                }  # Rotate clockwise


class Agent(object):

    def __init__(self, agent_id, start_pos, grid, env_name, num_agents, representation):
        """Superclass for all agents.

        Parameters
        ----------
        agent_id: (str)
            a unique id allowing the map to identify the agents
        start_pos: (np.ndarray)
            a 2d array indicating the x-y position of the agents
        grid: (2d array)
            a reference to this agent's view of the environment
        row_size: (int)
            how many rows up and down the agent can look
        col_size: (int)
            how many columns left and right the agent can look
        """
        self.agent_id = agent_id
        self.done = False
        self.pos = np.array(start_pos)
        # TODO(ev) change grid to env, this name is not very informative
        self.grid = grid
        self.reward_this_turn = 0
        self.collective_return = 0
        self.env_name = env_name
        self.update_agent_pos(start_pos)
        self.action_space = Discrete(5)

        if 'StagHunt' in self.env_name:
            if representation == 'one_hot':
                self.observation_space = Box(
                    0, 1, [(10+2*(num_agents-2)) * grid.shape[0]])
            else:
                self.observation_space = Box(0, 5, [(10+2*(num_agents-2))])
            self.gore_num = 0
            self.hare_num = 0


    def action_map(self, action_number):
        """Maps action_number to a desired action in the map"""
        return AGENT_ACTIONS[action_number]

    def get_total_actions(self):
        return len(AGENT_ACTIONS)

    def compute_reward(self):
        reward = self.reward_this_turn
        self.collective_return += reward
        self.reward_this_turn = 0
        return reward

    def set_pos(self, new_pos):
        self.pos = np.array(new_pos)

    def get_pos(self):
        return self.pos

    def get_done(self):
        return self.done

    def update_agent_pos(self, new_pos):
        """Updates the agents internal positions
        """
        ego_new_pos = new_pos  # self.translate_pos_to_egocentric_coord(new_pos)
        new_row, new_col = ego_new_pos

        # you can't walk through walls
        temp_pos = new_pos.copy()
        if new_row < 0 or new_row >= self.grid.shape[0] or new_col < 0 or new_col >= self.grid.shape[1]:
            temp_pos = self.get_pos()
        
        self.set_pos(temp_pos)
