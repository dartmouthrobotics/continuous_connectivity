#!/usr/bin/python

import rospy
from std_msgs.msg import *
from std_srvs.srv import *
from nav2d_msgs.msg import *
from nav_msgs.msg import *
from sensor_msgs.msg import *
from geometry_msgs.msg import *
import math
from gvgexploration.msg import *
from nav2d_navigator.msg import MoveToPosition2DActionGoal, MoveToPosition2DActionResult, ExploreActionResult
from actionlib_msgs.msg import GoalStatusArray, GoalID
from std_srvs.srv import Trigger
import numpy as np
from time import sleep
from threading import Thread, Lock
import copy
from os import path
import tf
import pickle
import os
from os.path import exists
from gvgexploration.srv import *
import project_utils as pu
from std_msgs.msg import String
from visualization_msgs.msg import Marker
INF = 1000000000000
NEG_INF = -1000000000000
MAX_ATTEMPTS = 2
RR_TYPE = 2
ROBOT_SPACE = 1
TO_RENDEZVOUS = 1
TO_FRONTIER = 0
FROM_EXPLORATION = 4
ACTIVE_STATE = 1  # This state shows that the robot is collecting messages
PASSIVE_STATE = -1  # This state shows  that the robot is NOT collecting messages
ACTIVE = 1  # The goal is currently being processed by the action server
SUCCEEDED = 3  # The goal was achieved successfully by the action server (Terminal State)
ABORTED = 4  # The goal was aborted during execution by the action server due to some failure (Terminal State)
LOST = 9  # An action client can determine that a goal is LOST. This should not be sent over the wire by an action
TURNING_ANGLE = np.deg2rad(45)
BUFFER_SIZE = 65536


class Robot:
    def __init__(self, robot_id, robot_type=0, base_stations=[], relay_robots=[], frontier_robots=[]):
        self.lock = Lock()
        self.robot_id = robot_id
        self.robot_type = robot_type
        self.base_stations = base_stations
        self.relay_robots = relay_robots
        self.frontier_robots = frontier_robots
        self.rendezvous_publishers = {}
        self.frontier_point = None
        self.previous_frontier_point = None
        self.robot_state = ACTIVE_STATE
        self.publisher_map = {}
        self.initial_data_count = 0
        self.is_exploring = False
        self.exploration_id = None
        self.karto_messages = {}
        self.last_map_update_time = rospy.Time.now().to_sec()
        self.last_buffered_data_time = rospy.Time.now().to_sec()
        self.goal_count = 0
        self.total_exploration_time = None
        self.moveTo_pub = None
        self.start_exploration = None
        self.move_to_stop = None
        self.cancel_explore_pub = None
        self.received_choices = {}
        self.vertex_descriptions = {}
        self.navigation_plan = None
        self.waiting_for_plan = False
        self.start_now = None
        self.robot_pose = None
        self.signal_strength = {}
        self.all_feedbacks = {}
        self.close_devices = []
        self.received_devices = 0
        self.move_attempt = 0
        self.is_initial_rendezvous_sharing = True
        self.received_choices = {}
        self.is_sender = False
        self.prev_slope = 0
        self.map_previous_pose = None
        self.map_prev_slope = 0
        self.is_initialization = True
        self.map_update_is_active = False
        self.others_active = False
        self.is_initial_data_sharing = True
        self.initial_receipt = True
        self.is_initial_move = True
        self.frontier_points = []
        self.frontier_data = []
        self.coverage = None
        self.exploration_data_senders = {}
        self.service_map = {}
        self.shared_data_srv_map = {}
        self.shared_point_srv_map = {}
        self.allocation_srv_map = {}
        self.conn_manager = {}
        self.is_active = False
        self.global_robot_pose = None

        self.exploration_time = rospy.Time.now().to_sec()
        self.candidate_robots = self.frontier_robots + self.base_stations
        self.debug_mode = rospy.get_param("/debug_mode")
        self.termination_metric = rospy.get_param("/termination_metric")
        self.robot_count = rospy.get_param("/robot_count")
        self.environment = rospy.get_param("/environment")
        self.graph_scale = rospy.get_param("/graph_scale")
        self.termination_metric = rospy.get_param("/termination_metric")
        self.max_exploration_time = rospy.get_param("/max_exploration_time")
        self.max_coverage = rospy.get_param("/max_coverage")
        self.max_common_coverage = rospy.get_param("/max_common_coverage")
        self.target_distance = rospy.get_param('/target_distance')
        self.target_angle = rospy.get_param('/target_angle')
        self.share_limit = rospy.get_param('/data_share_threshold')

        self.max_target_info_ratio=rospy.get_param("/max_target_info_ratio")
        self.method = rospy.get_param("/method")
        self.robot_count = rospy.get_param("/robot_count")
        self.run = rospy.get_param("/run")

        self.traveled_distance=[]
        self.previous_point=[]

        self.rate = rospy.Rate(0.1)
        self.min_hallway_width = rospy.get_param("/min_hallway_width".format(self.robot_id))
        self.comm_range = rospy.get_param("/comm_range".format(self.robot_id))
        self.point_precision = rospy.get_param("/point_precision".format(self.robot_id))
        self.min_edge_length = rospy.get_param("/min_edge_length".format(self.robot_id))

        rospy.Service("/robot_{}/shared_data".format(self.robot_id), SharedData, self.shared_data_handler,
                      buff_size=BUFFER_SIZE)
        self.auction_points_srv = rospy.Service("/robot_{}/auction_points".format(self.robot_id), SharedPoint,
                                                self.shared_point_handler, buff_size=BUFFER_SIZE)
        self.alloc_point_srv = rospy.Service("/robot_{}/allocated_point".format(self.robot_id), SharedFrontier,
                                             self.shared_frontier_handler, buff_size=BUFFER_SIZE)
        rospy.Subscriber('/robot_{}/initial_data'.format(self.robot_id), BufferedData, self.buffered_data_callback,
                         queue_size=1000)
        self.signal_strength_srv = rospy.ServiceProxy("/signal_strength".format(self.robot_id), HotSpot)
        self.data_size_pub = rospy.Publisher('/shared_data_size', DataSize, queue_size=10)
        rospy.logerr("Robot {}, Candidates: {}".format(self.robot_id, self.candidate_robots))
        for ri in self.candidate_robots:
            pub = rospy.Publisher("/roscbt/robot_{}/initial_data".format(ri), BufferedData, queue_size=1000)
            data_srv = rospy.ServiceProxy("/robot_{}/received_data".format(ri), SharedData, persistent=True)
            received_data_clt = rospy.ServiceProxy("/robot_{}/shared_data".format(ri), SharedData, persistent=True)
            action_points_clt = rospy.ServiceProxy("/robot_{}/auction_points".format(ri), SharedPoint, persistent=True)
            alloc_point_clt = rospy.ServiceProxy("/robot_{}/allocated_point".format(ri), SharedFrontier,
                                                 persistent=True)
            self.publisher_map[ri] = pub
            self.service_map[ri] = data_srv
            self.shared_data_srv_map[ri] = received_data_clt
            self.shared_point_srv_map[ri] = action_points_clt
            self.allocation_srv_map[ri] = alloc_point_clt
            self.conn_manager[ri] = rospy.Time.now().to_sec()

        self.karto_pub = rospy.Publisher("/robot_{}/karto_in".format(self.robot_id), LocalizedScan, queue_size=1000)
        self.chose_point_pub = rospy.Publisher("/chosen_point", ChosenPoint, queue_size=1000)
        self.auction_point_pub = rospy.Publisher("/auction_point", ChosenPoint, queue_size=1000)
        self.moveTo_pub = rospy.Publisher("/robot_{}/MoveTo/goal".format(self.robot_id), MoveToPosition2DActionGoal,
                                          queue_size=10)
        self.chose_point_pub = rospy.Publisher("/chosen_point", ChosenPoint, queue_size=1000)
        self.cancel_explore_pub = rospy.Publisher("/robot_{}/Explore/cancel".format(self.robot_id), GoalID,
                                                  queue_size=10)
        self.pose_publisher = rospy.Publisher("/robot_{}/cmd_vel".format(self.robot_id), Twist, queue_size=1)
        rospy.Subscriber("/robot_{}/map".format(self.robot_id), OccupancyGrid, self.map_callback)
        self.start_exploration = rospy.ServiceProxy('/robot_{}/StartExploration'.format(self.robot_id), Trigger)
        self.move_to_stop = rospy.ServiceProxy('/robot_{}/Stop'.format(self.robot_id), Trigger)
        rospy.Subscriber("/roscbt/robot_{}/signal_strength".format(self.robot_id), SignalStrength,
                         self.signal_strength_callback)
        rospy.Subscriber("/robot_{}/MoveTo/status".format(self.robot_id), GoalStatusArray, self.move_status_callback)
        rospy.Subscriber("/robot_{}/MoveTo/result".format(self.robot_id), MoveToPosition2DActionResult,
                         self.move_result_callback)
        rospy.Subscriber('/robot_{}/Explore/status'.format(self.robot_id), GoalStatusArray, self.exploration_callback)
        rospy.Subscriber('/robot_{}/Explore/result'.format(self.robot_id), ExploreActionResult,
                         self.exploration_result_callback)
        rospy.Subscriber("/robot_{}/navigator/plan".format(self.robot_id), GridCells, self.navigation_plan_callback)
        rospy.Subscriber('/karto_out'.format(self.robot_id), LocalizedScan, self.robots_karto_out_callback,
                         queue_size=1000)
        rospy.Subscriber("/robot_{}/pose".format(self.robot_id), Pose, callback=self.pose_callback)
        self.fetch_frontier_points = rospy.ServiceProxy('/robot_{}/frontier_points'.format(self.robot_id),
                                                        FrontierPoint)
        self.fetch_rendezvous_points = rospy.ServiceProxy('/robot_{}/rendezvous_points'.format(self.robot_id),
                                                          RendezvousPoints)
        rospy.Subscriber('/shutdown', String, self.save_all_data)
        rospy.Subscriber('/coverage'.format(self.robot_id), Coverage, self.coverage_callback)
        rospy.Subscriber('/robot_{}/navigator/markers'.format(self.robot_id), Marker, self.goal_callback)
        self.exploration_started = False
        self.is_shutdown_caller = False

        # robot identification sensor
        self.robot_range_pub = rospy.Publisher("/robot_{}/robot_ranges".format(self.robot_id), RobotRange, queue_size=5)
        self.received_msgs_ts={}
        self.robot_poses = {}
        self.trans_matrices = {}
        self.inverse_trans_matrices = {}
        self.failed_points = []


        all_robots = self.candidate_robots + [self.robot_id]
        for i in all_robots:
            s = "def a_" + str(i) + "(self, data): self.robot_poses[" + str(i) + "] = (data.pose.pose.position.x," \
                                                                                 "data.pose.pose.position.y," \
                                                                                 "(data.pose.pose.orientation.x,data.pose.pose.orientation.y,data.pose.pose.orientation.z,data.pose.pose.orientation.w)) "
            exec(s)
            exec("setattr(Robot, 'callback_pos_teammate" + str(i) + "', a_" + str(i) + ")")
            exec("rospy.Subscriber('/robot_" + str(
                i) + "/base_pose_ground_truth', Odometry, self.callback_pos_teammate" + str(i) + ", queue_size = 100)")

        # ======= pose transformations====================
        self.listener = tf.TransformListener()
        rospy.loginfo("Robot {} Initialized successfully!!".format(self.robot_id))

    def spin(self):
        r = rospy.Rate(0.05)
        while not rospy.is_shutdown():
            if self.is_exploring:
                # try:
                if not self.is_active:
                    self.is_active = True
                    self.exchange_data()
                    self.is_active = False
                # except Exception as e:
                #     pu.log_msg(self.robot_id, "Error: {}".format(e), self.debug_mode)
            r.sleep()

    def is_time_to_share(self, rid):
        now = rospy.Time.now().to_sec()
        diff = now - self.conn_manager[rid]
        return diff > self.share_limit

    def update_share_time(self, rid):
        self.conn_manager[rid] = rospy.Time.now().to_sec()

    def exchange_data(self):
        close_devices = self.get_close_devices()
        if close_devices:
            received_data = {}
            data_size = 0
            for rid in close_devices:
                sender_id = str(rid)
                if self.is_time_to_share(sender_id):
                    rid_data = self.create_buff_data(sender_id)
                    new_data = None
                    try:
                        new_data = self.shared_data_srv_map[sender_id](SharedDataRequest(req_data=rid_data))
                    except:
                        pass
                    self.update_share_time(sender_id)
                    if new_data:
                        received_data[sender_id] = new_data.res_data
                        data_size += self.get_data_size(new_data.res_data.data)
                    data_size += self.get_data_size(rid_data.data)
                    pu.log_msg(self.robot_id, "Shared data with: {}".format(sender_id), self.debug_mode)
                    self.delete_data_for_id(sender_id)
                    # except Exception as e:
                    #     pu.log_msg(self.robot_id, "Error in data sharing: {}".format(e), self.debug_mode)
                else:
                    pu.log_msg(self.robot_id, "Cant share with {} now. Last shared: {} secs ago".format(sender_id,
                                                                                                        rospy.Time.now().to_sec() -
                                                                                                        self.conn_manager[
                                                                                                            sender_id]),
                               self.debug_mode)
            if data_size:
                self.report_shared_data(data_size)
                self.process_received_data(received_data)


    def goal_callback(self,data):
        rospy.logerr('Received a new pose: {}'.format(data.pose.position))
        if len(self.previous_point)==0:
            self.previous_point= self.get_robot_pose()
        goal =[0.0]*2
        goal[pu.INDEX_FOR_X]=data.pose.position.x
        goal[pu.INDEX_FOR_X]=data.pose.position.y
        self.traveled_distance.append({'time': rospy.Time.now().to_sec(),'traved_distance': pu.D(self.previous_point, goal)})
        self.previous_point=goal


    def process_received_data(self, received_data):
        for rid, buff_data in received_data.items():
            # thread = Thread(target=self.process_data, args=(rid, buff_data,))
            # thread.start()
            self.process_data(rid, buff_data)

    def get_close_devices(self):
        ss_data = self.signal_strength_srv(HotSpotRequest(robot_id=str(self.robot_id)))
        data = ss_data.hot_spots
        signals = data.signals
        robots = []
        devices = []
        for rs in signals:
            robots.append([rs.robot_id, rs.rssi])
            devices.append(str(rs.robot_id))
        return set(devices)

    def create_buff_data(self, rid, is_alert=0):
        message_data = self.load_data_for_id(rid)
        pu.log_msg(self.robot_id, "Contains message data", self.debug_mode)
        buffered_data = BufferedData()
        buffered_data.msg_header.header.stamp = rospy.Time.now()
        buffered_data.msg_header.header.frame_id = '{}'.format(self.robot_id)
        buffered_data.msg_header.sender_id = str(self.robot_id)
        buffered_data.msg_header.receiver_id = str(rid)
        buffered_data.msg_header.topic = 'received_data'
        buffered_data.secs = []
        buffered_data.data = message_data

        buffered_data.alert_flag = is_alert
        return buffered_data

    def coverage_callback(self, data):
        self.coverage = data

    def exploration_result_callback(self, data):
        s = data.status.status
        pu.log_msg(self.robot_id, "Exploration status: {}".format(s), self.debug_mode)
        if s == ABORTED:
            self.initiate_exploration()

    def evaluate_exploration(self):
        its_time = False
        if self.coverage:
            if self.termination_metric == pu.MAXIMUM_EXPLORATION_TIME:
                time = rospy.Time.now().to_sec() - self.exploration_time
                its_time = time >= self.max_exploration_time * 60
            elif self.termination_metric == pu.TOTAL_COVERAGE:
                its_time = self.coverage.coverage >= self.max_coverage
            elif self.termination_metric == pu.FULL_COVERAGE:
                its_time = self.coverage.coverage >= self.coverage.expected_coverage
            elif self.termination_metric == pu.COMMON_COVERAGE:
                its_time = self.coverage.common_coverage >= self.max_common_coverage

        return its_time

    def report_shared_data(self, shared_size):
        data_size = DataSize()
        data_size.header.frame_id = '{}'.format(self.robot_id)
        data_size.header.stamp = rospy.Time.now()
        data_size.size = shared_size
        data_size.session_id = 'continuous'
        self.data_size_pub.publish(data_size)

    def get_data_size(self, buff_data):
        data_size = len(buff_data)  # sys.getsizeof(buff_data)
        return data_size

    def wait_for_map_update(self):
        r = rospy.Rate(0.1)
        while not self.is_time_to_moveon():
            r.sleep()

    ''' This publishes the newly computed rendezvous points for 10 seconds in an interval of 1 second'''

    def publish_rendezvous_points(self, rendezvous_poses, receivers, direction=1, total_exploration_time=0):
        rv_points = RendezvousPoints()
        rv_points.poses = rendezvous_poses
        rv_points.msg_header.header.frame_id = '{}'.format(self.robot_id)
        rv_points.msg_header.sender_id = str(self.robot_id)
        rv_points.msg_header.topic = 'rendezvous_points'
        rv_points.exploration_time = total_exploration_time
        rv_points.direction = direction
        for id in receivers:
            rv_points.msg_header.receiver_id = str(id)
            self.rendezvous_publishers[id].publish(rv_points)

    def robots_karto_out_callback(self, data):
        if data.robot_id - 1 == self.robot_id:
            for rid in self.candidate_robots:
                self.add_to_file(rid, [data])
            if self.is_initial_data_sharing:
                self.push_messages_to_receiver(self.candidate_robots)
                self.is_initial_data_sharing = False

    def map_callback(self, data):
        self.last_map_update_time = rospy.Time.now().to_sec()

    def move_robot_to_goal(self, goal, direction=1):
        id_val = "robot_{}_{}_{}".format(self.robot_id, self.goal_count, direction)
        move = MoveToPosition2DActionGoal()
        move.header.frame_id = '/robot_{}/map'.format(self.robot_id)
        goal_id = GoalID()
        goal_id.id = id_val
        move.goal_id = goal_id
        move.goal.target_pose.x = goal[0]
        move.goal.target_pose.y = goal[1]
        move.goal.target_distance = self.target_distance
        move.goal.target_angle = self.target_angle
        self.moveTo_pub.publish(move)
        self.goal_count += 1
        chosen_point = ChosenPoint()
        chosen_point.header.frame_id = '{}'.format(self.robot_id)
        chosen_point.x = goal[0]
        chosen_point.y = goal[1]
        chosen_point.direction = direction
        self.chose_point_pub.publish(chosen_point)
        self.robot_state = PASSIVE_STATE
        self.is_initial_move = False

    def move_status_callback(self, data):
        id_0 = "robot_{}_{}_{}".format(self.robot_id, self.goal_count - 1, TO_FRONTIER)
        if data.status_list:
            goal_status = data.status_list[0]
            if goal_status.goal_id.id:
                if goal_status.goal_id.id == id_0:
                    if goal_status.status == ACTIVE:
                        rospy.loginfo("Robot type, {} Heading to Frontier point...".format(self.robot_type))

    '''This callback receives the final result of the most recent navigation to the goal'''

    def move_result_callback(self, data):
        id_0 = "robot_{}_{}_{}".format(self.robot_id, self.goal_count - 1, TO_FRONTIER)
        if data.status:
            if data.status.status == ABORTED:
                if data.status.goal_id.id == id_0:
                    if self.move_attempt < MAX_ATTEMPTS:
                        self.rotate_robot()
                        self.move_robot_to_goal(self.frontier_point, TO_FRONTIER)
                        self.move_attempt += 1
                    else:
                        self.failed_points.append(self.frontier_point)
                        rv_ps = [p for p in self.frontier_points if p not in self.failed_points]
                        if rv_ps:
                            self.frontier_point = rv_ps[-1]
                            self.move_robot_to_goal(self.frontier_point, TO_FRONTIER)
                        else:  # start exploration anyways
                            self.initiate_exploration()
            elif data.status.status == SUCCEEDED:
                if data.status.goal_id.id == id_0:
                    pu.log_msg(self.robot_id, "Arrived at Frontier: {}".format(self.frontier_point), self.debug_mode)
                    self.initiate_exploration()

    def shared_data_handler(self, data):
        pu.log_msg(self.robot_id, "Received request from robot..", self.debug_mode)
        received_buff_data = data.req_data
        sender_id = received_buff_data.msg_header.header.frame_id
        self.update_share_time(sender_id)
        buff_data = self.create_buff_data(sender_id, is_alert=0)
        self.process_data(sender_id,received_buff_data)
        # thread = Thread(target=self.process_data, args=(sender_id, received_buff_data,))
        # thread.start()
        return SharedDataResponse(poses=[], res_data=buff_data)

    def compute_and_share_auction_points(self, auction_feedback, frontier_points):
        all_robots = list(auction_feedback)
        taken_poses = []
        for i in all_robots:
            pose_i = (auction_feedback[i][1].position.x, auction_feedback[i][1].position.y)
            conflicts = []
            for j in all_robots:
                if i != j:
                    pose_j = (auction_feedback[j][1].position.x, auction_feedback[j][1].position.y)
                    if pose_i == pose_j:
                        conflicts.append((i, j))
            rpose = auction_feedback[i][1]
            frontier = self.create_frontier(i, frontier_points[(rpose.position.x, rpose.position.y)])
            pu.log_msg(self.robot_id, "Sharing point with {}".format(i), self.debug_mode)
            res = self.allocation_srv_map[i](SharedFrontierRequest(frontier=frontier))
            pu.log_msg(self.robot_id, "Shared a frontier point with robot {}: {}".format(i, res), self.debug_mode)
            taken_poses.append(pose_i)
            for c in conflicts:
                conflicting_robot_id = c[1]
                feedback_for_j = self.all_feedbacks[conflicting_robot_id]
                rob_poses = feedback_for_j.poses
                rob_distances = feedback_for_j.distances
                remaining_poses = {}
                for k in range(len(rob_poses)):
                    p = (rob_poses[k].position.x, rob_poses[k].position.y)
                    if p != pose_i and p not in taken_poses:
                        remaining_poses[rob_distances[k]] = rob_poses[k]
                if remaining_poses:
                    next_closest_dist = min(list(remaining_poses))
                    auction_feedback[conflicting_robot_id] = (next_closest_dist, remaining_poses[next_closest_dist])
        return taken_poses

    def create_frontier(self, receiver, ridge):
        frontier = Frontier()
        frontier.msg_header.header.frame_id = '{}'.format(self.robot_id)
        frontier.msg_header.header.stamp = rospy.Time.now()
        frontier.msg_header.sender_id = str(self.robot_id)
        frontier.msg_header.receiver_id = str(receiver)
        frontier.msg_header.topic = 'allocated_point'
        frontier.frontier = ridge  #
        frontier.session_id = ''
        return frontier

    def request_and_share_frontiers(self, direction=0):
        frontier_point_response = self.fetch_frontier_points(FrontierPointRequest(count=len(self.candidate_robots) + 1))
        frontier_points = self.parse_frontier_response(frontier_point_response)
        if frontier_points:
            auction = self.create_auction(frontier_points)
            auction_feedback = {}
            for rid in self.candidate_robots:
                auction_response = self.shared_point_srv_map[rid](SharedPointRequest(req_data=auction))
                data = auction_response.res_data
                self.all_feedbacks[rid] = data
                m = len(data.distances)
                min_dist = max(data.distances)
                min_pose = None
                for i in range(m):
                    if min_dist >= data.distances[i]:
                        min_pose = data.poses[i]
                        min_dist = data.distances[i]
                auction_feedback[rid] = (min_dist, min_pose)
                taken_poses = self.compute_and_share_auction_points(auction_feedback, frontier_points)
                ridge = self.compute_next_frontier(taken_poses, frontier_points)
                new_point = [0.0] * 2
                if ridge:
                    new_point[pu.INDEX_FOR_X] = ridge.position.x
                    new_point[pu.INDEX_FOR_Y] = ridge.position.y
                else:
                    pose = taken_poses[0]
                    new_point[pu.INDEX_FOR_X] = pose[pu.INDEX_FOR_X]
                    new_point[pu.INDEX_FOR_Y] = pose[pu.INDEX_FOR_Y]
                new_point = pu.scale_down(new_point, self.graph_scale)
                self.frontier_point = new_point
                self.start_exploration_action(self.frontier_point)
            self.all_feedbacks.clear()
        else:
            rospy.logerr("Robot {}: No initial frontiers".format(self.robot_id))

    def compute_next_frontier(self, taken_poses, frontier_points):
        ridge = None
        robot_pose = self.get_robot_pose()
        dist_dict = {}
        for point in frontier_points:
            dist_dict[pu.D(robot_pose, point)] = point
        while not ridge and dist_dict:
            min_dist = min(list(dist_dict))
            closest = dist_dict[min_dist]
            if closest not in taken_poses:
                ridge = frontier_points[closest]
                break
            del dist_dict[min_dist]
        return ridge

    def create_auction(self, rendezvous_poses, distances=[]):
        auction = Auction()
        auction.msg_header.header.frame_id = '{}'.format(self.robot_id)
        auction.msg_header.sender_id = str(self.robot_id)
        auction.msg_header.topic = 'auction_points'
        auction.msg_header.header.stamp = rospy.Time.now()
        auction.session_id = ''
        auction.distances = distances
        auction.poses = []
        self.all_feedbacks.clear()
        for k, v in rendezvous_poses.items():
            pose = Pose()
            pose.position.x = k[pu.INDEX_FOR_X]
            pose.position.y = k[pu.INDEX_FOR_Y]
            auction.poses.append(pose)
        return auction

    def shared_frontier_handler(self, req):
        data = req.frontier
        pu.log_msg(self.robot_id, "Received new frontier point", self.debug_mode)
        self.frontier_ridge = data.frontier
        new_point = [0.0] * 2
        new_point[pu.INDEX_FOR_X] = self.frontier_ridge.position.x
        new_point[pu.INDEX_FOR_Y] = self.frontier_ridge.position.y
        new_point = pu.scale_down(new_point, self.graph_scale)
        self.frontier_point = new_point
        robot_pose = self.get_robot_pose()
        self.frontier_data.append(
            {'time': rospy.Time.now().to_sec(), 'distance_to_frontier': pu.D(robot_pose, new_point)})
        self.move_robot_to_goal(self.frontier_point, TO_FRONTIER)
        pu.log_msg(self.robot_id, "Received allocated points", self.debug_mode)
        return SharedFrontierResponse(success=1)

    def shared_point_handler(self, auction_data):
        pu.log_msg(self.robot_id, "Received auction", self.debug_mode)
        data = auction_data.req_data
        sender_id = data.msg_header.header.frame_id
        poses = data.poses
        if not poses:
            pu.log_msg(self.robot_id, "No poses received. Proceeding to my next frontier", self.debug_mode)
            self.start_exploration_action(self.frontier_point)
            return SharedPointResponse(auction_accepted=1, res_data=None)
        received_points = []
        distances = []
        robot_pose = self.get_robot_pose()
        robot_pose = pu.scale_up(robot_pose, self.graph_scale)
        for p in poses:
            received_points.append(p)
            point = (p.position.x, p.position.y,
                     self.get_elevation((p.orientation.x, p.orientation.y, p.orientation.z, p.orientation.w)))
            distance = pu.D(robot_pose, point)
            distances.append(distance)
        auction = Auction()
        auction.msg_header.header.frame_id = '{}'.format(self.robot_id)
        auction.msg_header.header.stamp = rospy.Time.now()
        auction.msg_header.sender_id = str(self.robot_id)
        auction.msg_header.receiver_id = str(sender_id)
        auction.msg_header.topic = 'auction_feedback'
        auction.poses = received_points
        auction.distances = distances
        auction.session_id = data.session_id
        return SharedPointResponse(auction_accepted=1, res_data=auction)

    def start_exploration_action(self, new_point, direction=0):
        if not self.frontier_point:
            self.previous_frontier_point = copy.deepcopy(self.frontier_point)
        self.frontier_point = new_point
        self.move_robot_to_goal(self.frontier_point, TO_FRONTIER)
        pu.log_msg(self.robot_id, "Going to frontier {} ..".format(self.frontier_point), self.debug_mode)

    def rotate_robot(self):
        deg_360 = math.radians(360)
        if self.robot_pose:
            while self.robot_pose[2] < deg_360:
                self.move_robot((0, 1))

    def move_robot(self, vel):
        vel_msg = Twist()
        vel_msg.linear.x = vel[0]
        vel_msg.angular.z = vel[1]
        self.pose_publisher.publish(vel_msg)

    def initiate_exploration(self):
        result = self.start_exploration()
        if result:
            self.exploration_time = rospy.Time.now()
            self.is_exploring = True
            self.heading_back = 0
            self.robot_state = ACTIVE_STATE

    def signal_strength_callback(self, data):
        signals = data.signals
        robots = []
        devices = []
        for rs in signals:
            robots.append([rs.robot_id, rs.rssi])
            devices.append(rs.robot_id)
        self.close_devices = devices

    def save_message(self,karto):
        rid = karto.robot_id -1
        ts = karto.scan.header.stamp.to_sec()
        should_save=False
        if rid not in self.received_msgs_ts:
            self.received_msgs_ts[rid]=ts
            should_save=True
        else:
            if self.received_msgs_ts[rid]<ts:
                self.received_msgs_ts[rid]=ts
                should_save=True
        if should_save:
            self.add_to_file(rid,[karto])
        return should_save

    def process_data(self, sender_id, buff_data):
        # self.lock.acquire()
        try:
            data_vals = buff_data.data
            self.last_map_update_time = rospy.Time.now().to_sec()
            counter = 0
            for scan in data_vals:
                if self.save_message(scan):
                    self.karto_pub.publish(scan)
                counter += 1
        except Exception as e:
            pu.log_msg(self.robot_id, "Error processing data: {}".format(e),self.debug_mode)
        # self.lock.release()

    def push_messages_to_receiver(self, receiver_ids, is_alert=0):
        for rid in receiver_ids:
            message_data = self.load_data_for_id(rid)
            buffered_data = BufferedData()
            buffered_data.msg_header.header.stamp = rospy.Time.now()
            buffered_data.msg_header.header.frame_id = '{}'.format(self.robot_id)
            buffered_data.msg_header.sender_id = str(self.robot_id)
            buffered_data.msg_header.receiver_id = str(rid)
            buffered_data.msg_header.topic = 'initial_data'
            buffered_data.secs = []
            buffered_data.data = message_data
            buffered_data.alert_flag = is_alert
            self.publisher_map[rid].publish(buffered_data)
            self.delete_data_for_id(rid)

    def buffered_data_callback(self, buff_data):
        sender_id = buff_data.msg_header.header.frame_id
        if sender_id in self.candidate_robots:
            if self.initial_receipt:
                self.push_messages_to_receiver([sender_id])
                self.process_data(sender_id,buff_data)
                # thread = Thread(target=self.process_data, args=(sender_id, buff_data,))
                # thread.start()
                pu.log_msg(self.robot_id, "Initial data from {}: {} files".format(sender_id, len(buff_data.data)),
                           self.debug_mode)
                self.initial_data_count += 1
                if self.initial_data_count == len(self.candidate_robots):
                    self.initial_receipt = False
                    if self.i_have_least_id():
                        self.wait_for_map_update()
                        pu.log_msg(self.robot_id, "Computing frontier points Active".format(self.robot_id),self.debug_mode)
                        self.request_and_share_frontiers()
                    else:
                        pu.log_msg(self.robot_id, "Waiting for frontier points...", self.debug_mode)
            else:
                pu.log_msg(self.robot_id, "Already initiated...", self.debug_mode)
                pass

    def get_available_points(self, points):
        available_points = []
        for p in points:
            if p not in self.received_choices:
                if len(self.received_choices):
                    for point in self.received_choices:
                        distance = math.sqrt((p[0] - point[0]) ** 2 + (p[1] - point[1]) ** 2)
                        if distance > ROBOT_SPACE:
                            available_points.append(p)
                else:
                    available_points.append(p)
        return available_points

    def i_have_least_id(self):
        id = int(self.robot_id)
        least = min([int(i) for i in self.candidate_robots])
        return id < least

    def navigation_plan_callback(self, data):
        if self.waiting_for_plan:
            self.navigation_plan = data.cells

    def start_exploration_callback(self, data):
        self.start_now = data

    def is_time_to_moveon(self):
        diff = rospy.Time.now().to_sec() - self.last_map_update_time
        return diff > 10

    def get_robot_pose(self):
        robot_pose = None
        count = 0
        while not robot_pose:
            try:
                self.listener.waitForTransform("robot_{}/map".format(self.robot_id),
                                               "robot_{}/base_link".format(self.robot_id), rospy.Time(),
                                               rospy.Duration(4.0))
                (robot_loc_val, rot) = self.listener.lookupTransform("robot_{}/map".format(self.robot_id),
                                                                     "robot_{}/base_link".format(self.robot_id),
                                                                     rospy.Time(0))
                robot_pose = (math.floor(robot_loc_val[0]), math.floor(robot_loc_val[1]), robot_loc_val[2])
                self.global_robot_pose = robot_pose
            except:
                count += 1
                sleep(1)
                # rospy.logerr("Robot {}: Can't fetch robot pose from tf".format(self.robot_id))
                if count > 5:
                    break

        return self.global_robot_pose

    def exploration_callback(self, data):
        if data.status_list:
            goal_status = data.status_list[0]
            gid = goal_status.goal_id.id
            if gid and self.exploration_id != gid:
                self.exploration_id = gid
                status = goal_status.status
                if status == ABORTED:
                    pu.log_msg(self.robot_id, "Robot stopped share data..", self.debug_mode)
                    self.move_to_stop()
                    self.start_exploration()

    def cancel_exploration(self):
        if self.exploration_id:
            pu.log_msg(self.robot_id, "Cancelling exploration...", self.debug_mode)
            goal_id = GoalID()
            goal_id.id = self.exploration_id
            self.cancel_explore_pub.publish(goal_id)
        else:
            pu.log_msg(self.robot_id, "Exploration ID not set...", self.debug_mode)

    def add_to_file(self, rid, data):
        # self.lock.acquire()
        try:
            if rid in self.karto_messages:
                self.karto_messages[rid] += data
            else:
                self.karto_messages[rid] = data
        except:
            pass
        # self.lock.release()
        return True

    def load_data_for_id(self, rid):
        message_data = []
        if rid in self.karto_messages:
            message_data = self.karto_messages[rid]
        return message_data

    def delete_data_for_id(self, rid):
        self.lock.acquire()
        if rid in self.karto_messages:
            del self.karto_messages[rid]
        self.lock.release()
        return True

    def pose_callback(self, msg):
        pose = (msg.x, msg.y, msg.theta)
        self.robot_pose = pose

    def theta(self, p, q):
        dx = q[0] - p[0]
        dy = q[1] - p[1]
        if dx == 0:
            dx = 0.000001
        return math.atan2(dy, dx)

    def D(self, p, q):
        dx = q[0] - p[0]
        dy = q[1] - p[1]
        return math.sqrt(dx ** 2 + dy ** 2)

    def parse_frontier_response(self, data):
        frontier_points = {}
        received_ridges = data.frontiers
        for p in received_ridges:
            frontier_points[(p.position.x, p.position.y)] = p
        return frontier_points

    def publish_robot_ranges(self):
        if len(self.robot_poses) == len(self.candidate_robots) + 1:
            self.generate_transformation_matrices()
            if len(self.trans_matrices) == len(self.candidate_robots) + 1:
                pose = self.robot_poses[self.robot_id]
                yaw = self.get_elevation(pose[2])
                robotV = np.asarray([pose[0], pose[1], yaw, 1])

                distances = []
                angles = []
                for rid in self.candidate_robots:
                    robot_c = self.get_robot_coordinate(rid, robotV)
                    distance = self.D(pose, robot_c)
                    angle = self.theta(pose, robot_c)
                    distances.append(distance)
                    angles.append(angle)
                robot_range = RobotRange()
                robot_range.distances = distances
                robot_range.angles = angles
                robot_range.robot_id = self.robot_id
                robot_range.header.stamp = rospy.Time.now()
                self.robot_range_pub.publish(robot_range)

    def get_elevation(self, quaternion):
        euler = tf.transformations.euler_from_quaternion(quaternion)
        yaw = euler[2]
        return yaw

    def generate_transformation_matrices(self):
        map_origin = (0, 0, 0, 1)
        for rid in self.candidate_robots + [self.robot_id]:
            p = self.robot_poses[int(rid)]
            theta = self.theta(map_origin, p)
            M = [[np.cos(theta), -1 * np.sin(theta), 0], [np.sin(theta), np.cos(theta), 0], [0, 0, 1]]
            T = copy.deepcopy(M)
            T.append([0, 0, 0, 1])
            yaw = self.get_elevation(p[2])
            T[0].append(p[0])
            T[1].append(p[1])
            T[2].append(yaw)
            tT = np.asarray(T)
            self.trans_matrices[rid] = tT
            tT_inv = np.linalg.inv(tT)
            self.inverse_trans_matrices[int(rid)] = tT_inv

    def get_robot_coordinate(self, rid, V):
        other_T = self.trans_matrices[rid]  # oTr2
        robot_T = self.inverse_trans_matrices[self.robot_id]  #
        cTr = other_T.dot(robot_T)
        # cTr_inv = np.linalg.inv(cTr)
        other_robot_pose = cTr.dot(V)
        return other_robot_pose.tolist()

    def save_all_data(self, data):
        pu.save_data(self.traveled_distance,'{}/traveled_distance_{}_{}_{}_{}_{}_{}.pickle'.format(self.method, self.environment,self.robot_count,self.run,self.termination_metric,self.robot_id,self.max_target_info_ratio))
        rospy.signal_shutdown('Robot {}: Received Shutdown Exploration complete!'.format(self.robot_id))


if __name__ == "__main__":
    # initializing a node
    rospy.init_node("robot_node", anonymous=True)
    robot_id = int(rospy.get_param("~robot_id", 0))
    robot_type = int(rospy.get_param("~robot_type", 0))
    base_stations = str(rospy.get_param("~base_stations", ''))
    relay_robots = str(rospy.get_param("~relay_robots", ''))
    frontier_robots = str(rospy.get_param("~frontier_robots", ''))

    relay_robots = relay_robots.split(',')
    frontier_robots = frontier_robots.split(',')
    base_stations = base_stations.split(',')

    rospy.loginfo("ROBOT_ID: {},RELAY ROBOTS: {}, FRONTIER ROBOTS: {}, BASE STATION: {}".format(robot_id, relay_robots,
                                                                                                frontier_robots,
                                                                                                base_stations))
    if relay_robots[0] == '':
        relay_robots = []
    if frontier_robots[0] == '':
        frontier_robots = []
    if base_stations[0] == '':
        base_stations = []
    robot = Robot(robot_id, robot_type=robot_type, base_stations=base_stations, relay_robots=relay_robots,
                  frontier_robots=frontier_robots)
    robot.spin()
