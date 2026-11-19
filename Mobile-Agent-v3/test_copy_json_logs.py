"""Regression checks for compact exports of both trajectory log schemas."""

import copy
import json
from pathlib import Path
import tempfile
import unittest

from copy_json_logs import compact_trajectory, copy_json_logs


def messages(label):
    return [{"role": "user", "content": [
        {"type": "text", "text": label},
        {"type": "image_url", "image_url": {
            "url": "data:image/png;base64,xxxxxxxx==",
        }},
    ]}]


class CompactTrajectoryTests(unittest.TestCase):
    def test_single_call_preserves_responses_actions_and_first_prompt(self):
        trajectory = [
            {"step": 0, "action": "{'action': 'click'}",
             "response": "<thinking>Open app</thinking><tool_call>{}</tool_call>",
             "messages": messages("first"), "time_consumption": 8.15},
            {"step": 1, "action": {"action": "wait"}, "is_attack": True,
             "response": "next response", "messages": messages("next")},
        ]
        original = copy.deepcopy(trajectory)
        compact = compact_trajectory(trajectory)
        self.assertEqual(compact[0]["messages"], original[0]["messages"])
        self.assertNotIn("messages", compact[1])
        self.assertNotIn("time_consumption", compact[0])
        for raw, exported in zip(original, compact):
            self.assertEqual(exported["response"], raw["response"])
            self.assertEqual(exported["action"], raw["action"])
        self.assertTrue(compact[1]["is_attack"])
        self.assertEqual(trajectory, original)

    def test_multi_call_keeps_independent_prompts_after_skipped_calls(self):
        trajectory = [
            {"step": 0, "action_response": " ",
             "action_messages": messages("skipped action"),
             "planning_response": "plan", "planning_messages": messages("plan"),
             "reflect_response": "", "reflect_messages": []},
            {"step": 1, "action": '{"action": "click", "coordinate": [280, 856]}',
             "action_response": "#", "action_messages": messages("action"),
             "planning_response": "", "planning_messages": messages("skipped plan"),
             "reflect_response": "##e.", "reflect_messages": messages("reflect")},
            {"step": 2, "action_response": "next action",
             "action_messages": messages("next action"),
             "planning_response": "next plan", "planning_messages": messages("next plan"),
             "reflect_response": "next reflect", "reflect_messages": messages("next reflect")},
        ]
        compact = compact_trajectory(trajectory)
        self.assertNotIn("action_messages", compact[0])
        self.assertEqual(compact[0]["planning_messages"], trajectory[0]["planning_messages"])
        self.assertEqual(compact[1]["action_messages"], trajectory[1]["action_messages"])
        self.assertEqual(compact[1]["reflect_messages"], trajectory[1]["reflect_messages"])
        self.assertNotIn("planning_messages", compact[1])
        for phase in ("action", "planning", "reflect"):
            self.assertNotIn(f"{phase}_messages", compact[2])
            for raw, exported in zip(trajectory, compact):
                self.assertEqual(exported[f"{phase}_response"], raw[f"{phase}_response"])

    def test_each_call_skips_invalid_and_assistant_only_prompts(self):
        invalid = [None, [], ["bad"], [{"content": "missing role"}],
                   [{"role": "user"}], [{"role": "assistant", "content": "answer"}]]
        for prefix in ("", "action_", "planning_", "reflect_"):
            with self.subTest(prefix=prefix):
                trajectory = [
                    {prefix + "messages": prompt, prefix + "response": "reply"}
                    for prompt in invalid
                ]
                trajectory.append({prefix + "messages": messages("valid"),
                                   prefix + "response": "reply"})
                compact = compact_trajectory(trajectory)
                for exported in compact[:-1]:
                    self.assertNotIn(prefix + "messages", exported)
                self.assertEqual(compact[-1][prefix + "messages"], messages("valid"))

    def test_legacy_logs_keep_step_aliases_and_non_dict_entries(self):
        trajectory = [None, {"step_id": 7, "action": "first", "messages": []},
                      {"step_index": 8, "action": "next", "messages": messages("valid")},
                      {"step_num": 9, "step_number": 9, "messages": messages("later")}]
        compact = compact_trajectory(trajectory)
        self.assertIsNone(compact[0])
        self.assertEqual(compact[1], {"step_id": 7, "action": "first"})
        self.assertEqual(compact[2]["messages"], messages("valid"))
        self.assertEqual(compact[3], {"step_num": 9, "step_number": 9})


class CopyJsonLogsTests(unittest.TestCase):
    def test_both_layouts_export_without_modifying_sources(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            output = root / "export"
            trajectories = {
                "model/task/run/0.json": [{"step": 0, "action": "click",
                                          "response": "reply", "messages": messages("single")}],
                "model/category/task/run/log_0_defense_-1.json": [
                    {"step": 1, "action": "click", "action_response": "action",
                     "action_messages": messages("action"), "planning_response": "",
                     "planning_messages": messages("skipped"), "reflect_response": "reflect",
                     "reflect_messages": messages("reflect")},
                ],
            }
            originals = {}
            for relative, trajectory in trajectories.items():
                path = source / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(trajectory), encoding="utf-8-sig")
                originals[relative] = path.read_bytes()
            metadata = source / "model/task/run/task_result.json"
            metadata.write_text('{"success": true}', encoding="utf-8")
            self.assertEqual(copy_json_logs(source, output), 2)
            self.assertFalse((output / metadata.relative_to(source)).exists())
            for relative, trajectory in trajectories.items():
                self.assertEqual((source / relative).read_bytes(), originals[relative])
                exported = json.loads((output / relative).read_text(encoding="utf-8"))
                self.assertEqual(exported, compact_trajectory(trajectory))


if __name__ == "__main__":
    unittest.main()
