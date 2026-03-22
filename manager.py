import json
import os
import time
import re


class ModManager:
    def __init__(self):
        self.mutes_file = "mutes.json"
        self.warns_file = "warns.json"
        self.rules_file = "rules.json"
        self.mutes = self._load(self.mutes_file, {})
        self.warns = self._load(self.warns_file, {})
        self.rules = self._load(self.rules_file, {})

    def _load(self, path, default):
        if os.path.exists(path):
            with open(path, "r") as f:
                try:
                    return json.load(f)
                except Exception:
                    return default
        return default

    def _save(self, path, data):
        with open(path, "w") as f:
            json.dump(data, f, indent=4)

    def is_muted(self, user_id):
        uid = str(user_id)
        if uid in self.mutes:
            exp = self.mutes[uid]
            if exp == -1 or time.time() < exp:
                return True, exp
            del self.mutes[uid]
            self._save(self.mutes_file, self.mutes)
        return False, None

    def parse_time(self, time_str):
        if time_str == "always":
            return -1, "forever"
        time_match = re.match(r"^(\d+)(m|h|d|mo|y)$", time_str.lower())
        if not time_match:
            return None, None

        val, unit = int(time_match.group(1)), time_match.group(2)
        mapping = {
            "m": (60, "minutes"),
            "h": (3600, "hours"),
            "d": (86400, "days"),
            "mo": (2592000, "months"),
            "y": (31536000, "years"),
        }
        secs, label = mapping[unit]
        return val * secs, f"{val} {label}"

    def warn_user(self, user_id, rule_id):
        uid, rid = str(user_id), str(rule_id)
        if rid not in self.rules:
            return None, "Rule ID not found."

        if uid not in self.warns:
            self.warns[uid] = {}
        self.warns[uid][rid] = self.warns[uid].get(rid, 0) + 1
        x = self.warns[uid][rid]

        try:
            minutes = eval(self.rules[rid]["formula"], {"x": x})
            expiry = (
                (time.time() + minutes * 60)
                if minutes > 0
                else (None if minutes == 0 else -1)
            )
            if expiry:
                self.mutes[uid] = expiry
            self._save(self.mutes_file, self.mutes)
            self._save(self.warns_file, self.warns)
            return {
                "count": x,
                "mute_mins": minutes,
                "name": self.rules[rid]["name"],
            }, None
        except Exception as e:
            return None, str(e)

    def unwarn_user(self, user_id, rule_id):
        uid, rid = str(user_id), str(rule_id)
        if rid not in self.rules:
            return None, "Rule ID not found."

        if uid not in self.warns:
            return None, "User never be warned"
        self.warns[uid][rid] = max(0, self.warns[uid].get(rid, 0) - 1)
        x = self.warns[uid][rid]

        try:
            # minutes = eval(self.rules[rid]["formula"], {"x": x})
            # expiry = (
            #    (time.time() + minutes * 60)
            #    if minutes > 0
            #    else (None if minutes == 0 else -1)
            # )
            # if expiry:
            #    self.mutes[uid] = expiry
            # self._save(self.mutes_file, self.mutes)
            self._save(self.warns_file, self.warns)
            return {
                "count": x,
                "mute_mins": 0,
                "name": self.rules[rid]["name"],
            }, None
        except Exception as e:
            return None, str(e)

    def set_mute(self, user_id, seconds):
        uid = str(user_id)
        self.mutes[uid] = -1 if seconds == -1 else (time.time() + seconds)
        self._save(self.mutes_file, self.mutes)

    def unmute(self, user_id):
        uid = str(user_id)
        if uid in self.mutes:
            del self.mutes[uid]
            self._save(self.mutes_file, self.mutes)
            return True
        return False
