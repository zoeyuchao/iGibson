from gibson2.tasks.task_base import BaseTask
import pybullet as p
from gibson2.scenes.igibson_indoor_scene import InteractiveIndoorScene
from gibson2.scenes.gibson_indoor_scene import StaticIndoorScene
from gibson2.termination_conditions.max_collision import MaxCollision
from gibson2.termination_conditions.timeout import Timeout
from gibson2.termination_conditions.out_of_bound import OutOfBound
from gibson2.reward_functions.map_explore_reward import MapExploreReward
from gibson2.reward_functions.collision_reward import CollisionReward

from gibson2.utils.utils import l2_distance, rotate_vector_3d, cartesian_to_polar
from gibson2.utils.utils import quat_pos_to_mat


import numpy as np

import cv2
from sklearn.neighbors import NearestNeighbors
import logging
import pdb
from sklearn.neighbors import KDTree

# For simplicity, we set the global map's coordinate same as the env's coordinate.
# i.e. using env.robots[0].get_position() would give you the position in global map coord.

class globalMap():
    def __init__(self, input_hw, dsample_rate=1.0, max_num=10000):
        # init_pos: The starting position (in env coordinate) of the current scene
        # init_pose: The starting camera pose (extrinsics)
        # input_hw: The height and width of input rgbd image
        # dsample_rate: Down-sample rate to store the map (point cloud)
        self.input_hw = input_hw
        self.dsample_rate = int(dsample_rate)
        self.max_num = max_num

        self.map_points = None
        self.smap_points = None

    def random_sample_pc(self, points, rate):
        mask = (np.random.rand(points.shape[0]) < (1.0 / rate))
        sampled_points = points[mask]
        return sampled_points

    def local_to_global(self, points, pose):
        # Transform the camera coord points into world (env) coord.
        # points: [N, 3], pose: [4, 4] [R, T] format
        h_points = np.concatenate([points, np.ones_like(points)[:,-1:]], -1)
        gpoints = pose.dot(np.transpose(h_points))[:3,:]
        return np.transpose(gpoints)

    def remove_duplicate(self, points, sampled=False, eps=0.1):
        # remove the existed points in global map
        # Note: here we only use the distance threshold since we assume perfect depth and pose
        if not sampled:
            kdt = KDTree(self.map_points[:,:3], leaf_size=30, metric='euclidean')
        else:
            kdt = KDTree(self.smap_points[:,:3], leaf_size=30, metric='euclidean')

        dist, ind = kdt.query(points[:,:3], k=1, return_distance=True)
        mask = (dist < eps).squeeze(-1)
        new_points = points[~mask]
        return new_points

    def merge_local_pc(self, pc, pose):
        # Merge the current observed point cloud (in global coord) to the global map
        pc_flat = np.reshape(pc, [-1,3])
        lpoints_in_gcoord = self.local_to_global(pc_flat, pose)

        if not (self.map_points is None):
            # Get the newly observed points and merge into the global map (both full map and sampled map)
            new_points = self.remove_duplicate(lpoints_in_gcoord)
            snew_points = self.random_sample_pc(new_points, self.dsample_rate)

            self.map_points = np.concatenate([self.map_points, new_points], 0)
            self.smap_points = np.concatenate([self.smap_points, snew_points], 0)
            increase_ratio = new_points.shape[0] / (self.input_hw[0] * self.input_hw[1])
        else:
            self.map_points = np.copy(lpoints_in_gcoord)
            self.smap_points = np.copy(self.random_sample_pc(lpoints_in_gcoord, self.dsample_rate))
            increase_ratio = 1.0
        return increase_ratio

    def global_to_local(self, map, tgt_pose):
        # return transformed global map into local frame coordinate
        # tgt_pose: target frame's camera pose (4x4 RT extrinsics matrix)
        world2cam = np.linalg.inv(tgt_pose)
        gmap_in_lcoord = self.local_to_global(map, world2cam)
        return gmap_in_lcoord

    def get_input_map(self, pose):
        """
        Transform global map into one agent's "local" view

        :param pose: agent's current pose
        :return: input_map to PointNet for current robot, shape: [max_num, 3]
        """
        N = self.smap_points.shape[0]
        if N > self.max_num:
            print("Map overflow!")
            input_map = np.copy(self.smap_points[N-self.max_num:, :])
        else:
            input_map = np.concatenate([self.smap_points, np.tile(self.smap_points[:1,:], (self.max_num-N, 1))], 0)
        input_map = self.global_to_local(input_map, pose)
        return input_map


class RoomExplorationTask(BaseTask):
    """
    Room Exploration Task
    The goal is to explore the whole room.
    """

    def __init__(self, env):
        super(RoomExplorationTask, self).__init__(env)
        self.termination_conditions = [
            MaxCollision(self.config),
            Timeout(self.config),
            OutOfBound(self.config),
        ]
        self.reward_functions = [
            CollisionReward(self.config),
            MapExploreReward(self.config),
        ]

        self.random_init = self.config.get("random_pos", False)
        if not self.random_init:
            self.initial_pos_all = np.array(self.config.get('initial_pos', [[0, 0, 0]]))
            self.initial_rpy_all = np.zeros((self.num_robots, 3))
            assert len(self.initial_pos_all.shape) == 2 and self.initial_pos_all.shape[0] == self.num_robots, \
                'initial_pos must be consistent with robot num'
            assert len(self.initial_rpy_all.shape) == 2 and self.initial_rpy_all.shape[0] == self.num_robots, \
                'initial_pos must be consistent with robot num'

        self.img_h = self.config.get('image_height', 128)
        self.img_w = self.config.get('image_width', 128)
        self.gmap = globalMap((self.img_h, self.img_w), 8, self.config.get('n_max_points', 10000))
        self.increase_ratios = np.zeros(env.num_robots)

        self.floor_num = 0

    def reset_scene(self, env):
        """
        Reset all scene objects.

        :param env: environment instance
        """
        env.scene.reset_scene_objects()
        self.gmap.map_points, self.gmap.smap_points = None, None

    def sample_initial_pose(self, env):
        """
        Sample robot initial pose

        :param env: environment instance
        :return: initial pose
        """
        _, initial_pos = env.scene.get_random_point(floor=self.floor_num)
        initial_rpy = np.array([0, 0, np.random.uniform(0, np.pi * 2)])
        return initial_pos, initial_rpy

    def reset_agent(self, env):
        """
        Reset robot initial pose.

        :param env: environment instance
        """
        if self.random_init:
            max_trials = 100
            # cache pybullet state
            # TODO: p.saveState takes a few seconds, need to speed up
            state_id = p.saveState()
            for _ in range(max_trials):
                reset_success = np.zeros(self.num_robots, dtype=bool)
                initial_pos = np.zeros((self.num_robots, 3))
                initial_rpy = np.zeros((self.num_robots, 3))
                for robot_id in range(self.num_robots):
                    initial_pos[robot_id], initial_rpy[robot_id] = self.sample_initial_pose(env)
                    reset_success[robot_id] = env.test_valid_position(env.robots[robot_id], initial_pos[robot_id], initial_rpy[robot_id])
                p.restoreState(state_id)
                if np.all(reset_success):
                    break
            if not np.all(reset_success):
                logging.warning("WARNING: Failed to reset robot without collision")
            for robot_id in range(self.num_robots):
                env.land(env.robots[robot_id], initial_pos[robot_id], initial_rpy[robot_id])
                env.robots[robot_id].cur_position = initial_pos[robot_id]
            p.removeState(state_id)
        else:
            for i in range(self.num_robots):
                env.land(env.robots[i], self.initial_pos_all[i], self.initial_rpy_all[i])
                env.robots[i].cur_position = self.initial_pos_all[i]
        for reward_function in self.reward_functions:
            reward_function.reset(self, env)

    def global_to_local(self, env, pos):
        """
        Convert a 3D point in global frame to agent's local frame

        :param env: environment instance
        :param pos: a 3D point in global frame
        :return: the same 3D point in agent's local frame
        """
        return rotate_vector_3d(pos - env.robots[0].get_position(),
                                *env.robots[0].get_rpy())

    def step(self, env):
        """
        Use current state to merge the point cloud into global map, calculate the increase ratio.
        """
        obs = env.get_state()
        for robot_id in range(env.num_robots):
            pc = obs['pc'][robot_id]
            pos = env.robots[robot_id].get_position()
            quat = env.robots[robot_id].get_orientation()
            pose = quat_pos_to_mat(pos, quat)
            increase_ratio = self.gmap.merge_local_pc(pc, pose)
            self.increase_ratios[robot_id] = increase_ratio

    def get_task_obs(self, env):
        """
        Get task-specific observation, here we need to get each agent's "local" global map.

        :param env: environment instance
        :return: input map to PointNet for each robot, [num_robots, max_num, 3]
        """
        task_obs = []
        for robot_id in range(env.num_robots):
            pos = env.robots[robot_id].get_position()
            quat = env.robots[robot_id].get_orientation()
            pose = quat_pos_to_mat(pos, quat)
            local_global_map = self.gmap.get_input_map(pose)
            task_obs.append(local_global_map)
        return np.array(task_obs)
