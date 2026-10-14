"""ROS 2 node that gives a robot hypnos memory.

Subscribes:  /memory/observation (std_msgs/String)   "what the robot perceived"
             /memory/action      (std_msgs/String)   "action -> result"
             /memory/query       (std_msgs/String)   a question
             /robot/docked       (std_msgs/Bool)     True -> sleep (consolidate)
Publishes:   /memory/answer      (std_msgs/String)   recalled memory for the last query

Run:  python ros2_memory_node.py      (needs rclpy from a ROS 2 install, and hypnos)
"""
import threading

import rclpy
from rclpy.node import Node
from std_msgs.msg import Bool, String

from hypnos.integrations.agents import RobotMemory


class MemoryNode(Node):
    def __init__(self):
        super().__init__('hypnos_memory')
        name = self.declare_parameter('robot_name', 'robot1').value
        self.mem = RobotMemory(f'robot-{name}', session=self.get_clock().now().to_msg().sec.__str__())
        self.pub = self.create_publisher(String, '/memory/answer', 10)
        self.create_subscription(String, '/memory/observation', lambda m: self.mem.observe(m.data, sensor='ros'), 50)
        self.create_subscription(String, '/memory/action', self.on_action, 50)
        self.create_subscription(String, '/memory/query', self.on_query, 10)
        self.create_subscription(Bool, '/robot/docked', self.on_docked, 10)

    def on_action(self, msg):
        action, _, result = msg.data.partition('->')
        self.mem.act(action.strip(), result.strip())

    def on_query(self, msg):
        def work():                                    # recall takes ~100s of ms: keep it off the executor thread
            self.pub.publish(String(data=self.mem.recall(msg.data) or 'nothing relevant in memory'))
        threading.Thread(target=work, daemon=True).start()

    def on_docked(self, msg):
        if msg.data:
            threading.Thread(target=self.mem.sleep, daemon=True).start()


def main():
    rclpy.init()
    rclpy.spin(MemoryNode())


if __name__ == '__main__':
    main()
