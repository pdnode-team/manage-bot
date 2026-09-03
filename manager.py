import tomllib
from datetime import datetime, timedelta
from typing import Any, Dict, Optional, Tuple, cast
from sqlmodel import Session, select, desc, create_engine, text

from models import User, WarningRecord, ModLog, PendingConfirmation, AutoRule, NewRulesState


class ModManager:
    CONFIRM_EXPIRY_MINUTES = 30

    def __init__(self):
        self.engine = create_engine("sqlite:///database.db")
        raw_rules = self._load_config("rules")
        self.rules: Dict[str, Any] = raw_rules if isinstance(raw_rules, dict) else {}
        # ───────── New Rules Lockdown (persistent) ─────────
        self.bot_ids: set[int] = set()
        self._lockdown_dm_cooldown: dict[int, float] = {}  # user_id -> last DM timestamp
        self._new_rules_active = False  # in-memory cache, refreshed from DB
        self._load_lockdown_state()

    def _load_lockdown_state(self):
        """Load persistent lockdown state from DB on startup."""
        try:
            with Session(self.engine) as session:
                state = session.get(NewRulesState, 1)
                if state is None:
                    # Seed the single row so future UPDATEs work
                    session.add(NewRulesState(id=1, active=False))
                    session.commit()
                    self._new_rules_active = False
                else:
                    self._new_rules_active = bool(state.active)
                    if state.active:
                        print(
                            f"[lockdown] recovered active state from DB: "
                            f"activated_at={state.activated_at} "
                            f"by={state.activated_by_name}"
                        )
        except Exception as e:
            print(f"[lockdown] WARNING: failed to load state, defaulting inactive: {e}")
            self._new_rules_active = False

    # ───────── New Rules Lockdown ─────────

    def set_bot_ids(self, ids: list[int]):
        self.bot_ids = set(ids)

    def activate_new_rules(self, actor_id: int, actor_name: str):
        """Lockdown: only users who /agree-new-rules can send (bots exempt).
        Persists to DB so state survives bot restarts.
        """
        now = datetime.now()
        self._new_rules_active = True
        with Session(self.engine) as session:
            state = session.get(NewRulesState, 1)
            if state is None:
                state = NewRulesState(id=1)
                session.add(state)
            state.active = True
            state.activated_at = now
            state.activated_by = actor_id
            state.activated_by_name = actor_name
            session.add(state)
            session.exec(text("UPDATE user SET agreed_new_rules = 0"))
            session.commit()

    def deactivate_new_rules(self):
        """Lift lockdown. Persists to DB."""
        self._new_rules_active = False
        with Session(self.engine) as session:
            state = session.get(NewRulesState, 1)
            if state is not None:
                state.active = False
                session.add(state)
                session.commit()

    def agree_new_rules(self, user_id: int):
        """Mark a user as having agreed. Persisted on the User row."""
        with Session(self.engine) as session:
            user = session.get(User, user_id)
            if not user:
                user = User(zulip_id=user_id, username=f"User_{user_id}")
            user.agreed_new_rules = True
            session.add(user)
            session.commit()

    def has_agreed_new_rules(self, user_id: int) -> bool:
        with Session(self.engine) as session:
            user = session.get(User, user_id)
            return bool(user and user.agreed_new_rules)

    def is_new_rules_active(self) -> bool:
        return self._new_rules_active

    def can_send_lockdown_dm(self, user_id: int, cooldown_secs: int = 60) -> bool:
        """Rate-limit lockdown DMs to prevent spam."""
        import time
        now = time.time()
        last = self._lockdown_dm_cooldown.get(user_id, 0)
        if now - last >= cooldown_secs:
            self._lockdown_dm_cooldown[user_id] = now
            return True
        return False

    def _load_config(self, item: str = ""):
        try:
            with open("config.toml", "rb") as f:
                config = tomllib.load(f)
                return config.get(item) if item else config
        except (FileNotFoundError, tomllib.TOMLDecodeError) as e:
            print(f"ERROR Loading Config: {e}")
            return None

    # ───────── Mute / Unmute ─────────

    def is_muted(self, user_id: int) -> Tuple[bool, Optional[float]]:
        with Session(self.engine) as session:
            user = session.get(User, user_id)
            if not user or not user.is_muted:
                return False, None

            if user.mute_until:
                if datetime.now() > user.mute_until:
                    user.is_muted = False
                    user.mute_until = None
                    session.add(user)
                    session.commit()
                    return False, None
                return True, user.mute_until.timestamp()

            return True, -1  # permanent

    def unmute(self, user_id: int):
        with Session(self.engine) as session:
            user = session.get(User, user_id)
            if user and user.is_muted:
                user.is_muted = False
                user.mute_until = None
                session.add(user)
                session.commit()
                return True
            return False

    def set_mute(self, user_id: int, seconds: int):
        """Set mute duration, taking max so it never shortens existing mutes."""
        with Session(self.engine) as session:
            user = session.get(User, user_id)
            if not user:
                user = User(zulip_id=user_id, username=f"User_{user_id}")

            user.is_muted = True
            if seconds == -1:
                user.mute_until = None  # permanent
            else:
                new_until = datetime.now() + timedelta(seconds=seconds)
                if user.mute_until and user.mute_until > new_until:
                    pass  # keep existing longer mute
                else:
                    user.mute_until = new_until

            session.add(user)
            session.commit()

    def parse_time(self, time_str: str) -> Tuple[Optional[int], str]:
        import re
        ts = time_str.lower().strip()
        if not ts or ts in ["always", "forever", "inf", "-1"]:
            return -1, "forever"

        match = re.match(r"^(\d+)\s*([smhd]?)$", ts)
        if not match:
            return None, "invalid format"

        val_str, unit = match.groups()
        val = int(val_str)
        multipliers = {"s": 1, "m": 60, "": 60, "h": 3600, "d": 86400}
        seconds = val * multipliers.get(unit, 60)
        label = f"{val}{unit if unit else 'm'}"
        return seconds, label

    # ───────── Warn / Unwarn ─────────

    def warn_user(
        self, user_id: int, rule_id: str, reason: str = "No reason provided",
        actor_id: int = 0, actor_name: str = "Unknown",
    ) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
        rules = self.rules
        if not rules:
            return None, "Rules not loaded"

        parts = rule_id.split(".")
        if len(parts) != 2:
            return None, "Invalid Rule ID"
        category, sub_id = parts

        try:
            cat_data = cast(Dict[str, Any], rules[category])
            rule = cast(Dict[str, Any], cat_data[sub_id])
        except (KeyError, TypeError):
            return None, f"Rule {rule_id} not found"

        with Session(self.engine) as session:
            user = session.get(User, user_id)
            if not user:
                user = User(zulip_id=user_id, username=f"User_{user_id}")
                session.add(user)

            new_warn = WarningRecord(
                type=rule_id, reason=reason, user_id=user_id,
                actor_id=actor_id, actor_name=actor_name,
            )
            session.add(new_warn)
            session.flush()

            stmt = select(WarningRecord).where(
                WarningRecord.user_id == user_id, WarningRecord.type == rule_id
            )
            x = len(session.exec(stmt).all())

            try:
                formula = rule.get("formula", "0")
                minutes = eval(formula, {"x": x})

                if minutes != 0:
                    user.is_muted = True
                    if minutes > 0:
                        user.mute_until = datetime.now() + timedelta(minutes=minutes)
                    else:
                        user.mute_until = None  # permanent

                session.add(user)
                session.commit()

                return {"count": x, "mute_mins": minutes, "name": rule["name"]}, None
            except Exception as e:
                session.rollback()
                return None, str(e)

    def unwarn_user(
        self, user_id: int, rule_id: str
    ) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
        with Session(self.engine) as session:
            stmt = (
                select(WarningRecord)
                .where(WarningRecord.user_id == user_id, WarningRecord.type == rule_id)
                .order_by(desc(WarningRecord.id))
            )
            last_warning = session.exec(stmt).first()
            if not last_warning:
                return None, "User has no warning records for this rule."

            session.delete(last_warning)

            count_stmt = select(WarningRecord).where(
                WarningRecord.user_id == user_id, WarningRecord.type == rule_id
            )
            new_x = len(session.exec(count_stmt).all())

            user = session.get(User, user_id)
            if user:
                if new_x == 0:
                    user.is_muted = False
                    user.mute_until = None
                elif new_x > 0:
                    try:
                        cat, sub = rule_id.split(".")
                        rule = cast(Dict[str, Any], self.rules.get(cat, {})).get(sub, {})
                        formula = rule.get("formula", "0")
                        new_minutes = eval(formula, {"x": new_x})
                        if new_minutes != 0:
                            user.is_muted = True
                            if new_minutes > 0:
                                new_until = datetime.now() + timedelta(minutes=new_minutes)
                                if not (user.mute_until and user.mute_until > new_until):
                                    user.mute_until = new_until
                            else:
                                user.mute_until = None
                        else:
                            user.is_muted = False
                            user.mute_until = None
                    except Exception as e:
                        print(f"ERROR recalculating mute: {e}")
                session.add(user)
            session.commit()

            cat, sub = rule_id.split(".")
            rule_name = (
                cast(Dict[str, Any], self.rules.get(cat, {}))
                .get(sub, {})
                .get("name", "Unknown")
            )
            return {"count": new_x, "mute_mins": 0, "name": rule_name}, None

    # ───────── Original Actor Lookup ─────────

    def get_last_mute_actor(self, user_id: int) -> Optional[Dict[str, Any]]:
        """Find who issued the most recent mute for this user via ModLog."""
        with Session(self.engine) as session:
            stmt = (
                select(ModLog)
                .where(ModLog.target_id == user_id, ModLog.action == "mute")
                .order_by(desc(ModLog.timestamp))
            )
            log = session.exec(stmt).first()
            if log:
                return {"id": log.actor_id, "name": log.actor_name}
            return None

    def get_last_warn_actor(self, user_id: int, rule_id: str) -> Optional[Dict[str, Any]]:
        """Find who issued the most recent warn for this user+rule."""
        with Session(self.engine) as session:
            stmt = (
                select(WarningRecord)
                .where(WarningRecord.user_id == user_id, WarningRecord.type == rule_id)
                .order_by(desc(WarningRecord.id))
            )
            record = session.exec(stmt).first()
            if record and record.actor_id:
                return {"id": record.actor_id, "name": record.actor_name}
            # Fallback: check ModLog
            stmt2 = (
                select(ModLog)
                .where(
                    ModLog.target_id == user_id,
                    ModLog.action == "warn",
                    ModLog.details.like(f"Rule {rule_id}%"),
                )
                .order_by(desc(ModLog.timestamp))
            )
            log = session.exec(stmt2).first()
            if log:
                return {"id": log.actor_id, "name": log.actor_name}
            return None

    # ───────── Pending Confirmations ─────────

    def create_pending(
        self, action_type: str, actor_id: int, actor_name: str,
        target_id: int, target_name: str,
        duration: Optional[str] = None,
        duration_seconds: Optional[int] = None,
        rule_id: Optional[str] = None,
        original_actor_id: Optional[int] = None,
        original_actor_name: Optional[str] = None,
    ) -> int:
        now = datetime.now()
        pending = PendingConfirmation(
            action_type=action_type,
            actor_id=actor_id,
            actor_name=actor_name,
            target_id=target_id,
            target_name=target_name,
            duration=duration,
            duration_seconds=duration_seconds,
            rule_id=rule_id,
            original_actor_id=original_actor_id,
            original_actor_name=original_actor_name,
            timestamp=now,
            expires_at=now + timedelta(minutes=self.CONFIRM_EXPIRY_MINUTES),
        )
        with Session(self.engine) as session:
            session.add(pending)
            session.commit()
            session.refresh(pending)
            return pending.id

    def get_pending(self, target_id: int, action_type: str) -> Optional[PendingConfirmation]:
        """Get the most recent non-expired pending confirmation."""
        self._cleanup_expired()
        with Session(self.engine) as session:
            stmt = (
                select(PendingConfirmation)
                .where(
                    PendingConfirmation.target_id == target_id,
                    PendingConfirmation.action_type == action_type,
                    PendingConfirmation.expires_at > datetime.now(),
                )
                .order_by(desc(PendingConfirmation.timestamp))
            )
            return session.exec(stmt).first()

    def remove_pending(self, pending_id: int):
        with Session(self.engine) as session:
            pending = session.get(PendingConfirmation, pending_id)
            if pending:
                session.delete(pending)
                session.commit()

    def _cleanup_expired(self):
        with Session(self.engine) as session:
            stmt = select(PendingConfirmation).where(
                PendingConfirmation.expires_at <= datetime.now()
            )
            expired = session.exec(stmt).all()
            for p in expired:
                session.delete(p)
            if expired:
                session.commit()

    def list_pending(self) -> list[dict]:
        """List all non-expired pending confirmations."""
        self._cleanup_expired()
        with Session(self.engine) as session:
            stmt = (
                select(PendingConfirmation)
                .where(PendingConfirmation.expires_at > datetime.now())
                .order_by(desc(PendingConfirmation.timestamp))
            )
            result = []
            for p in session.exec(stmt).all():
                result.append({
                    "id": p.id,
                    "action_type": p.action_type,
                    "actor_name": p.actor_name,
                    "target_name": p.target_name,
                    "duration": p.duration,
                    "rule_id": p.rule_id,
                    "expires_at": p.expires_at,
                })
            return result

    # ───────── Status / Stats ─────────

    def get_all_mutes(self) -> Dict[int, float]:
        with Session(self.engine) as session:
            stmt = select(User).where(User.is_muted == True)
            users = session.exec(stmt).all()

            now = datetime.now()
            result: Dict[int, float] = {}
            for u in users:
                if u.mute_until and now > u.mute_until:
                    u.is_muted = False
                    u.mute_until = None
                    session.add(u)
                else:
                    result[u.zulip_id] = u.mute_until.timestamp() if u.mute_until else -1.0

            session.commit()
            return result

    def get_user_status(self, user_id: int) -> Dict[str, int]:
        with Session(self.engine) as session:
            stmt = select(WarningRecord).where(WarningRecord.user_id == user_id)
            results = session.exec(stmt).all()
            stats: Dict[str, int] = {}
            for record in results:
                stats[record.type] = stats.get(record.type, 0) + 1
            return stats

    # ───────── Audit Log ─────────

    def log_action(
        self, actor_id: int, actor_name: str, action: str,
        target_id: int, target_name: str, details: str,
    ):
        with Session(self.engine) as session:
            log = ModLog(
                actor_id=actor_id, actor_name=actor_name,
                action=action, target_id=target_id,
                target_name=target_name, details=details,
            )
            session.add(log)
            session.commit()

    def get_user_logs(self, user_id: int, limit: int = 20) -> list[dict]:
        with Session(self.engine) as session:
            stmt = (
                select(ModLog)
                .where(ModLog.target_id == user_id)
                .order_by(desc(ModLog.timestamp))
                .limit(limit)
            )
            logs = session.exec(stmt).all()
            return [
                {
                    "id": log.id, "timestamp": log.timestamp,
                    "actor_name": log.actor_name, "action": log.action,
                    "details": log.details,
                }
                for log in logs
            ]

    def get_recent_logs(self, limit: int = 50) -> list[dict]:
        with Session(self.engine) as session:
            stmt = (
                select(ModLog)
                .order_by(desc(ModLog.timestamp))
                .limit(limit)
            )
            logs = session.exec(stmt).all()
            return [
                {
                    "id": log.id, "timestamp": log.timestamp,
                    "actor_name": log.actor_name, "action": log.action,
                    "target_name": log.target_name, "details": log.details,
                }
                for log in logs
            ]

    # ───────── Auto-Rule CRUD ─────────

    def create_auto_rule(
        self, name: str|None, target_type: str, target_value: str,
        scope_stream_id: int|None, scope_topics: str|None,
        pattern: str, action: str, created_by: int,
    ) -> int:
        from datetime import datetime
        rule = AutoRule(
            name=name,
            target_type=target_type,
            target_value=target_value,
            scope_stream_id=scope_stream_id,
            scope_topics=scope_topics,
            pattern=pattern,
            action=action,
            created_by=created_by,
            created_at=str(datetime.now()),
            enabled=1,
        )
        with Session(self.engine) as session:
            session.add(rule)
            session.commit()
            session.refresh(rule)
            return rule.id

    def update_auto_rule(self, rule_id: int, **kwargs) -> bool:
        allowed = {"name", "target_type", "target_value", "scope_stream_id",
                   "scope_topics", "pattern", "action", "enabled"}
        with Session(self.engine) as session:
            rule = session.get(AutoRule, rule_id)
            if not rule:
                return False
            for k, v in kwargs.items():
                if k in allowed:
                    setattr(rule, k, v)
            session.add(rule)
            session.commit()
            return True

    def delete_auto_rule(self, rule_id: int) -> bool:
        with Session(self.engine) as session:
            rule = session.get(AutoRule, rule_id)
            if not rule:
                return False
            session.delete(rule)
            session.commit()
            return True

    def get_auto_rule(self, rule_id: int) -> dict|None:
        with Session(self.engine) as session:
            rule = session.get(AutoRule, rule_id)
            if not rule:
                return None
            return {
                "id": rule.id,
                "name": rule.name,
                "target_type": rule.target_type,
                "target_value": rule.target_value,
                "scope_stream_id": rule.scope_stream_id,
                "scope_topics": rule.scope_topics,
                "pattern": rule.pattern,
                "action": rule.action,
                "created_by": rule.created_by,
                "created_at": rule.created_at,
                "enabled": bool(rule.enabled),
            }

    def list_auto_rules(self) -> list[dict]:
        with Session(self.engine) as session:
            stmt = select(AutoRule).order_by(AutoRule.id)
            rules = session.exec(stmt).all()
            return [
                {
                    "id": r.id,
                    "name": r.name,
                    "target_type": r.target_type,
                    "target_value": r.target_value,
                    "scope_stream_id": r.scope_stream_id,
                    "scope_topics": r.scope_topics,
                    "pattern": r.pattern,
                    "action": r.action,
                    "enabled": bool(r.enabled),
                }
                for r in rules
            ]

    # ───────── Auto-Rule Matching ─────────

    def match_auto_rules(
        self, user_id: int, role: int, stream_id: int|None,
        topic: str|None, content: str,
    ) -> dict|None:
        """Find the first matching enabled auto-rule.
        Allow rules are checked first and take priority over action rules."""
        import re as re_module
        with Session(self.engine) as session:
            stmt = (
                select(AutoRule)
                .where(AutoRule.enabled == 1)
                .order_by(AutoRule.id)
            )
            rules = session.exec(stmt).all()

        allow_rule = None
        action_rule = None
        for r in rules:
            # Check target
            if r.target_type == "user":
                if str(user_id) != r.target_value:
                    continue
            elif r.target_type == "role":
                if r.target_value == "moderator":
                    if role < 300:
                        continue
                else:
                    continue
            else:
                continue

            # Check scope
            if r.scope_stream_id is not None:
                if stream_id is None or r.scope_stream_id != stream_id:
                    continue
            if r.scope_topics:
                import json
                try:
                    topic_list = json.loads(r.scope_topics)
                    if topic and topic not in topic_list:
                        continue
                    if not topic and topic_list:
                        continue
                except (json.JSONDecodeError, TypeError):
                    pass

            # Check pattern
            try:
                if not re_module.search(r.pattern, content):
                    continue
            except re_module.error:
                continue

            # Matched!
            rule_dict = {
                "id": r.id, "name": r.name,
                "action": r.action, "pattern": r.pattern,
            }
            if r.action == "allow":
                allow_rule = rule_dict
                break
            if action_rule is None:
                action_rule = rule_dict

        if allow_rule:
            return allow_rule
        return action_rule
