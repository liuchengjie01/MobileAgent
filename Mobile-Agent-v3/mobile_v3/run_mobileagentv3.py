import os
import re
import subprocess
from typing import Any, Dict, List, Optional
import uuid
import json
import time
import argparse
from PIL import Image
from datetime import datetime

from foreground_hook import ForegroundAppHook
from attack_util import load_hook_configuration, start_overlay_attack_flow, start_popup_sms_flow
from utils.mobile_agent_e import (
    InfoPool, 
    Manager, 
    Executor, 
    Notetaker, 
    ActionReflector,
    INPUT_KNOW
)

from utils.call_mobile_agent_e import GUIOwlWrapper


class MobileAgentRunner:
    def __init__(self, adb_path=None, hdc_path=None, device_id="emulator-5554", api_key="", base_url="", model="", coor_type="rel", if_notetaker=True, max_step=30, output_path=""):
        """
        初始化 Agent 核心组件（仅初始化一次）
        """
        if adb_path and hdc_path:
            raise ValueError("adb_path and hdc_path cannot be provided at the same time. Please specify only one of them.")
        
        # 1. 设备控制器
        if adb_path:
            from utils.android_controller import AndroidController
            self.controller = AndroidController(adb_path)
            self.adb_path = adb_path
        else:
            from utils.harmonyos_controller import HarmonyOSController
            self.controller = HarmonyOSController(hdc_path)
            self.adb_path = hdc_path
        self.device_id = device_id
        # 2. 模型推理封装与各 Role 实例（无状态工具类，单例常驻）
        self.vllm = GUIOwlWrapper(api_key, base_url, model)
        self.manager = Manager()
        self.executor = Executor()
        self.notetaker = Notetaker()
        self.action_reflector = ActionReflector()

        self.coor_type = coor_type
        self.if_notetaker = if_notetaker
        self.max_step = max_step
        self.output_path = output_path

    def reset_device_state(self):
        """
        在任务执行前后重置设备物理状态，避免上一任务残留页面/键盘干扰下一任务
        """
        try:
            self.controller.back()
            time.sleep(0.5)
            self.controller.back()
            time.sleep(0.5)
            self.controller.home()
            time.sleep(1.0)
        except Exception as e:
            print(f"[Device Reset Warning] Failed to reset device: {e}")

    def _save_task_result(self, save_path, instruction, hit_step_limit):
        task_result_path = os.path.join(save_path, "task_result.json")
        current_time = datetime.now()
        formatted_time = current_time.strftime("%Y-%m-%d %H:%M:%S.%f")
        task_result_data = {
            "goal": instruction,
            "finish_dtime": formatted_time,
            "hit_step_limit": hit_step_limit
        }
        with open(task_result_path, 'w', encoding='utf-8') as json_file:
            json.dump(task_result_data, json_file, ensure_ascii=False, indent=4)

    def _get_package_activity(self):
        cmd = f"{self.adb_path} -s {self.device_id} shell dumpsys window | findstr mCurrentFocus"
        try:
            result = subprocess.run(
                cmd,
                shell=True,
                capture_output=True,
                text=True,
                timeout=5,
                encoding='utf-8'  # Windows 可能需要 GBK
            )
            
            if result.returncode == 0 and result.stdout.strip():
                output = result.stdout.strip()
                match = re.search(r'(\S+)/(\S+?)(?=\s|}|$)', output)
                if match:
                    package = match.group(1)
                    activity = match.group(2)
                    return package, activity
                
                # 备用匹配：可能格式是 Window{... com.android.chrome/.MainActivity}
                match = re.search(r'Window\{[^}]*\s+([^\s/]+)/([^\s}]+)', output)
                if match:
                    return match.group(1), match.group(2)
        except subprocess.TimeoutExpired:
            print("ADB 命令超时")
        except Exception as e:
            print(f"ADB 执行失败: {e}")
        return "", ""

    def run_instruction(self, instruction, is_attack=0, data=None, add_info=None, rollout_id=1, log_path="." + os.sep + "logs"):
        """
        运行单个 Instruction, 包含完整闭环交互
        """
        # 1. 任务前置清理：回桌面，收起键盘
        self.reset_device_state()
        
        # 2. 初始化攻击过程
        if is_attack==1:
            hook: Optional[ForegroundAppHook] = None
            hook, httpd = None, None
            hook_cfg_config = load_hook_configuration(config_path=f'mobile_v3{os.sep}attack_config.yaml')
            if data.get("use_dynamic", "") == "Overlay Attack":
                hook, httpd = start_overlay_attack_flow(adb_path=self.adb_path, scenario=data, overlay_cfg=hook_cfg_config.get('overlay', {}), task_id=str(data.get("id", 1)))
            elif args.is_attack and data.get("use_dynamic", "") == "Popup SMS":
                hook = start_popup_sms_flow(adb_path=self.adb_path, scenario=data, task_id=str(data.get("id", 1)))

        # 3. 独立日志目录
        if not os.path.exists(log_path):
            os.makedirs(log_path, exist_ok=True)
        
        now = datetime.now()
        time_str = now.strftime("%Y%m%d_%H%M%S")
        save_path = log_path
        image_save_path = log_path

        # 4. 任务级状态池：全新实例，阻断上下文记忆污染
        info_pool = InfoPool(
            additional_knowledge_manager=add_info,
            additional_knowledge_executor=INPUT_KNOW,
            err_to_manager_thresh=2
        )
        info_pool.instruction = instruction

        message_manager = None
        local_image_dir2 = None
        task_completed = False

        message_file = os.path.join(save_path, f"{rollout_id}.json")
        message_data = []
        # {"name": "manager", "messages": message_manager, "response": output_planning, "step_id": step+1}
        for step in range(self.max_step):
            step_start_time = time.time()
            package, activity = self._get_package_activity()
            # 确定当前 step 的截图路径
            if step == 0:
                current_time = datetime.now()
                formatted_time = current_time.strftime(f'%Y-%m-%d-{current_time.hour * 3600 + current_time.minute * 60 + current_time.second}-{str(uuid.uuid4().hex[:8])}')
                local_image_dir = os.path.join(image_save_path, f"step_{rollout_id}_{step:02d}.png")
            else:
                local_image_dir = local_image_dir2

            # 获取截图
            for _ in range(5):
                if not self.controller.get_screenshot(local_image_dir):
                    print("Get screenshot failed, retry.")
                    time.sleep(5)
                else:
                    break

            width, height = Image.open(local_image_dir).size

            # 异常计数与 Plan 标志位处理
            info_pool.error_flag_plan = False
            err_to_manager_thresh = info_pool.err_to_manager_thresh
            if len(info_pool.action_outcomes) >= err_to_manager_thresh:
                latest_outcomes = info_pool.action_outcomes[-err_to_manager_thresh:]
                count = sum(1 for outcome in latest_outcomes if outcome in ["B", "C"])
                if count == err_to_manager_thresh:
                    info_pool.error_flag_plan = True

            # 决策是否跳过 Manager
            skip_manager = False
            if not info_pool.error_flag_plan and len(info_pool.action_history) > 0:
                if info_pool.action_history[-1]['action'] == 'invalid':
                    skip_manager = True

            # --- Manager 阶段 ---
            if not skip_manager:
                print("\n### Manager ... ###\n")
                prompt_planning = self.manager.get_prompt(info_pool)
                output_planning, message_manager, raw_response, planning_message = self.vllm.predict_mm(
                    prompt_planning,
                    [local_image_dir]
                )
                if not raw_response:
                    raise RuntimeError('Error calling vLLM in planning phase.')

            # message_save_path = os.path.join(save_path, f"step_{step+1}")
            # os.makedirs(message_save_path, exist_ok=True)

            action_object_str = ""
            parsed_result_planning = self.manager.parse_response(output_planning)
            info_pool.completed_plan = parsed_result_planning['completed_subgoal']
            info_pool.plan = parsed_result_planning['plan']

            print('Completed subgoal: ' + info_pool.completed_plan)
            print('Planning thought: ' + parsed_result_planning['thought'])
            print('Plan: ' + info_pool.plan, "\n")

            # 检测是否整体完成
            if "Finished" in info_pool.plan.strip() and len(info_pool.plan.strip()) < 15:
                print("Instruction finished, stop the process.")
                self._save_task_result(save_path, instruction, hit_step_limit=0.0)
                task_completed = True

                step_info = {
                    "step": step,
                    "time_consumption": time.time() - step_start_time,
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "log_prob_avg": 0.0,
                    "package": package,
                    "activity": activity,
                    "action": action_object_str,
                    "action_response": "",
                    "action_messages": [],
                    "planning_response": output_planning,
                    "planning_messages": planning_message,
                    "task_completed": task_completed
                }
                message_data.append(step_info)
                break

            # --- Operator 阶段 ---
            print("\n### Operator ... ###\n")
            prompt_action = self.executor.get_prompt(info_pool)
            output_action, message_operator, raw_response, action_message = self.vllm.predict_mm(
                prompt_action,
                [local_image_dir],
            )

            if not raw_response:
                raise RuntimeError('Error calling LLM in operator phase.')
            
            parsed_result_action = self.executor.parse_response(output_action)
            action_thought = parsed_result_action['thought']
            action_object_str = parsed_result_action['action']
            action_description = parsed_result_action['description']

            info_pool.last_action_thought = action_thought
            info_pool.last_summary = action_description

            if (not action_thought) or (not action_object_str):
                print('Action prompt output is not in the correct format.')
                info_pool.last_action = {"action": "invalid"}
                info_pool.action_history.append({"action": "invalid"})
                info_pool.summary_history.append(action_description)
                info_pool.action_outcomes.append("C")
                info_pool.error_descriptions.append("invalid action format, do nothing.")
                local_image_dir2 = local_image_dir

                step_info = {
                    "step": step,
                    "time_consumption": time.time() - step_start_time,
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "log_prob_avg": 0.0,
                    "package": package,
                    "activity": activity,
                    "action": action_object_str,
                    "action_response": output_action,
                    "action_messages": action_message,
                    "planning_response": output_planning,
                    "planning_messages": planning_message
                }
                message_data.append(step_info)
                continue

            action_object_str = action_object_str.replace("```", "").replace("json", "").strip()
            print('Thought: ' + action_thought)
            print('Action: ' + action_object_str)
            print('Action description: ' + action_description)

            try:
                action_object = json.loads(action_object_str)
                operator_response = f"### Thought ###\n{action_thought}\n\n### Action ###\n{action_object}\n\n### Description ###\n{action_description}"

                # 任务直接给出文本回答时结束
                if action_object['action'] == "answer":
                    # message_file = os.path.join(message_save_path, "operator.json")
                    # message_data = {"name": "operator", "messages": message_operator, "response": operator_response, "step_id": step+1}
                    # with open(message_file, 'w', encoding='utf-8') as json_file:
                    #     json.dump(message_data, json_file, ensure_ascii=False, indent=4)

                    answer_content = action_object.get('text', '')
                    print(f"Instruction finished, answer: {answer_content}, stop the process.")
                    self._save_task_result(save_path, instruction, hit_step_limit=0.0)
                    task_completed = True

                    step_info = {
                        "step": step,
                        "time_consumption": time.time() - step_start_time,
                        "prompt_tokens": 0,
                        "completion_tokens": 0,
                        "log_prob_avg": 0.0,
                        "package": package,
                        "activity": activity,
                        "action": action_object_str,
                        "action_response": output_action,
                        "action_messages": action_message,
                        "planning_response": output_planning,
                        "planning_messages": planning_message,
                        "task_completed": task_completed
                    }
                    message_data.append(step_info)
                    break

                # 坐标变换 (归一化坐标转绝对坐标)
                if self.coor_type != "abs":
                    if "coordinate" in action_object:
                        action_object['coordinate'] = [int(action_object['coordinate'][0] / 1000 * width), int(action_object['coordinate'][1] / 1000 * height)]
                    if "coordinate2" in action_object:
                        action_object['coordinate2'] = [int(action_object['coordinate2'][0] / 1000 * width), int(action_object['coordinate2'][1] / 1000 * height)]

                # 执行动作
                if action_object['action'] == "click":
                    self.controller.tap(action_object['coordinate'][0], action_object['coordinate'][1])
                elif action_object['action'] == "swipe":
                    self.controller.slide(action_object['coordinate'][0], action_object['coordinate'][1], action_object['coordinate2'][0], action_object['coordinate2'][1])
                elif action_object['action'] == "type":
                    self.controller.type(action_object['text'])
                elif action_object['action'] == "system_button":
                    if action_object['button'] == "Back":
                        self.controller.back()
                    elif action_object['button'] == "Home":
                        self.controller.home()

            except Exception as e:
                info_pool.last_action = {"action": "invalid"}
                info_pool.action_history.append({"action": "invalid"})
                info_pool.summary_history.append(action_description)
                info_pool.action_outcomes.append("C")
                info_pool.error_descriptions.append("invalid action format, do nothing.")
                local_image_dir2 = local_image_dir
                print(f'Execute Action Error: {e}')

                step_info = {
                    "step": step,
                    "time_consumption": time.time() - step_start_time,
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "log_prob_avg": 0.0,
                    "package": package,
                    "activity": activity,
                    "action": action_object_str,
                    "action_response": output_action,
                    "action_messages": action_message,
                    "planning_response": output_planning,
                    "planning_messages": planning_message
                }
                message_data.append(step_info)
                continue

            # message_file = os.path.join(message_save_path, "operator.json")
            # message_data = {"name": "operator", "messages": message_operator, "response": operator_response, "step_id": step+1}
            # with open(message_file, 'w', encoding='utf-8') as json_file:
            #     json.dump(message_data, json_file, ensure_ascii=False, indent=4)

            info_pool.last_action = json.loads(action_object_str)

            if step == 0:
                time.sleep(8)
            time.sleep(2)

            # 获取动作执行后的截图
            current_time = datetime.now()
            formatted_time = current_time.strftime(f'%Y-%m-%d-{current_time.hour * 3600 + current_time.minute * 60 + current_time.second}-{str(uuid.uuid4().hex[:8])}')
            local_image_dir2 = os.path.join(image_save_path, f"step_{rollout_id}_{step+1:02d}.png")

            for _ in range(5):
                if not self.controller.get_screenshot(local_image_dir2):
                    print("Get screenshot failed, retry.")
                    time.sleep(5)
                else:
                    break

            # --- Action Reflector 阶段 ---
            print("\n### Action Reflector ... ###\n")
            prompt_action_reflect = self.action_reflector.get_prompt(info_pool)
            output_action_reflect, message_reflector, raw_response, reflect_message = self.vllm.predict_mm(
                prompt_action_reflect,
                [local_image_dir, local_image_dir2],
            )

            # message_file = os.path.join(message_save_path, "reflector.json")
            # message_data = {"name": "reflector", "messages": message_reflector, "response": output_action_reflect, "step_id": step+1}
            # with open(message_file, 'w', encoding='utf-8') as json_file:
            #     json.dump(message_data, json_file, ensure_ascii=False, indent=4)

            parsed_result_action_reflect = self.action_reflector.parse_response(output_action_reflect)
            outcome = parsed_result_action_reflect['outcome']
            error_description = parsed_result_action_reflect['error_description']
            progress_status = info_pool.completed_plan

            if "A" in outcome:
                action_outcome = "A"
            elif "B" in outcome:
                action_outcome = "B"
            elif "C" in outcome:
                action_outcome = "C"
            else:
                raise ValueError("Invalid outcome:", outcome)

            print('Action reflection outcome: ' + action_outcome)
            print('Action reflection error description: ' + error_description)
            print('Action reflection progress status: ' + progress_status, "\n")

            info_pool.action_history.append(json.loads(action_object_str))
            info_pool.summary_history.append(action_description)
            info_pool.action_outcomes.append(action_outcome)
            info_pool.error_descriptions.append(error_description)
            info_pool.progress_status = progress_status


            # --- NoteKeeper 阶段 ---
            if action_outcome == "A" and self.if_notetaker:
                print("\n### NoteKeeper ... ###\n")
                prompt_note = self.notetaker.get_prompt(info_pool)
                output_note, message_notekeeper, raw_response, _ = self.vllm.predict_mm(
                    prompt_note,
                    [local_image_dir2],
                )

                # message_file = os.path.join(message_save_path, "notekeeper.json")
                # message_data = {"name": "notekeeper", "messages": message_notekeeper, "response": output_note, "step_id": step+1}
                # with open(message_file, 'w', encoding='utf-8') as json_file:
                #     json.dump(message_data, json_file, ensure_ascii=False, indent=4)

                parsed_result_note = self.notetaker.parse_response(output_note)
                important_notes = parsed_result_note['important_notes']
                info_pool.important_notes = important_notes

                print('Important notes: ' + important_notes, "\n")

            # --- logging ---
            step_info = {
                "step": step,
                "time_consumption": time.time() - step_start_time,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "log_prob_avg": 0.0,
                "package": package,
                "activity": activity,
                "action": action_object_str,
                "action_response": output_action,
                "action_messages": action_message,
                "planning_response": output_planning,
                "planning_messages": planning_message,
                "reflect_response": output_action_reflect,
                "reflect_messages": reflect_message
            }
            if os.path.exists(os.path.join("signal_files", f'{data.get("id", 1)}.txt')):
                os.remove(os.path.join("signal_files", f'{data.get("id", 1)}.txt'))
                step_info["is_attack"] = True
            message_data.append(step_info)


        # 超过步数限制退出
        if not task_completed:
            print(f"Task reached max step limit ({self.max_step}), terminating.")
            self._save_task_result(save_path, instruction, hit_step_limit=1.0)
        with open(message_file, 'w', encoding='utf-8') as fp:
            json.dump(message_data, fp, ensure_ascii=False, indent=2)
        if hook:
            hook.stop()
        if httpd:
            httpd.shutdown()
            httpd.server_close()
        # 任务后置清理：回桌面
        self.reset_device_state()


def load_dataset(dataset_path: str) -> List[Dict[str, Any]]:
    """Load scenario dataset from jsonl file."""
    with open(dataset_path, "r", encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]

    
if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description="Run Mobile-Agent-v3 with a given model and instruction"
    )
    parser.add_argument("--adb_path", type=str, default='adb')
    parser.add_argument("--hdc_path", type=str)   
    parser.add_argument("--api_key", type=str)
    parser.add_argument("--base_url", type=str)
    parser.add_argument("--model", type=str)
    parser.add_argument("--is-attack", type=int, default=0)
    # 1. iic/GUI-Owl-7B
    # 2. Qwen/Qwen3-VL-30B-A3B-Instruct
    # 3. ZhipuAI/AutoGLM-Phone-9B
    parser.add_argument("--instruction", type=str)
    parser.add_argument("--add_info", type=str, default="")
    parser.add_argument("--coor_type", type=str, default="abs")
    parser.add_argument("--notetaker", type=bool, default=False)

    # task
    parser.add_argument("--benchmark_path", type=str)
    parser.add_argument("--num-rollouts", type=int, default=1)
    args = parser.parse_args()
    dataset = load_dataset(args.benchmark_path)
    dataset = dataset[70:75]
    skip_task = [61,67]
    dataset = [entry for entry in dataset if entry.get("id") not in skip_task]
    runner = MobileAgentRunner(
        adb_path=args.adb_path,
        api_key=args.api_key,
        base_url=args.base_url,
        model=args.model,
        coor_type=args.coor_type,
        if_notetaker=args.notetaker
    )
    for data in dataset:
        target_dir = f'dpo_logs{os.sep}{args.model}{os.sep}{str(data.get("id", 1))}{os.sep}{str(datetime.now().strftime("%Y%m%d_%H%M%S"))}'
        os.makedirs(target_dir, exist_ok=True)
        for i in range(args.num_rollouts):
            print('#'* 50 + f'--task: {data.get("id", 1)}, rollnum: {i}--' + '#'*50)
            runner.run_instruction(
                instruction=data['prompt'],
                is_attack=args.is_attack,
                add_info="",
                log_path=target_dir,
                data=data,
                rollout_id=i
            )
