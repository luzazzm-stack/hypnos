# hypnos for robots

The `robotics` profile remembers:
- where objects were seen, and when
- map and layout facts
- skills that worked, and failures with their causes
- calibration values and safety notes

Facts go stale on their own schedule:

| Fact type | Goes stale after |
|---|---|
| Object locations | about 1 hour |
| Calibration | 14 days |
| Map facts | 30 days |

A stale fact is still returned, marked `STALE`, so the robot knows to look again before acting.

```python
from hypnos.integrations.agents import RobotMemory

mem = RobotMemory("robot-arm1", session="shift-2026-09-26")
mem.observe("red_mug on kitchen_table_2 at x=1.20 y=0.45", sensor="cam_front", pose=(1.2, 0.45, 0.0))
mem.act("grasp red_mug", "gripper closed at 38mm", success=True)
mem.sleep()                                   # e.g. when docked or idle
print(mem.where_is("red_mug"))
```

**Evidence:** every observation carries `obs:<sensor>@<time>` evidence. That's the robot version of R2 ("every fact cites evidence").

**Local service:** `RobotMemory` works in-process by default. If you use it through the service (`use_service=True`), set `stay_running = true` in `~/.hypnos/config.toml`, so the service doesn't stop when no coding agent is open.

**Where it fits:** use it for **episodic and semantic memory**: what happened, what worked, where things were. Don't put it in the real-time control loop; recall takes hundreds of milliseconds.

**ROS 2:** see [`examples/ros2_memory_node.py`](../../examples/ros2_memory_node.py). It's a node that:
- logs `std_msgs/String` observations and actions
- answers questions on a service-style topic
- sleeps when the robot reports it is docked
