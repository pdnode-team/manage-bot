import tomllib
from datetime import datetime, timedelta
from typing import Any, Dict, Optional, Tuple, cast
from sqlalchemy import Engine
from sqlmodel import Session, select, desc
from loguru import logger

# 导入你刚才定义的模型
from models import User, WarningRecord 

class ModManager:
    def __init__(self, engine: Engine):
        self.engine = engine
        raw_rules = self._load_config("rules")
        self.rules: Dict[str, Any] = raw_rules if isinstance(raw_rules, dict) else {}

    def _load_config(self, item: str = ""):
        try:
            with open("config.toml", "rb") as f:
                config = tomllib.load(f)
                return config.get(item) if item else config
        except (FileNotFoundError, tomllib.TOMLDecodeError) as e:
            logger.critical(f"ERROR Loading Config: {e}")
            return None

    def is_muted(self, user_id: int) -> Tuple[bool, Optional[float]]:
        """检查用户是否被禁言"""
        logger.debug("This user has been checked for account suspension status.", {"user_id": user_id})
        with Session(self.engine) as session:
            user = session.get(User, user_id)
            if not user or not user.is_muted:
                return False, None
            
            # 检查是否过期
            if user.mute_until:
                logger.success(f"User {user_id} has been automatically unmuted.")
                if datetime.now() > user.mute_until:
                    # 自动解封
                    user.is_muted = False
                    user.mute_until = None
                    session.add(user)
                    session.commit()
                    return False, None
                return True, user.mute_until.timestamp()
            
            # 如果 is_muted 为 True 但没有时间，则是永久禁言 (-1)
            return True, -1

    def warn_user(self, user_id: int, rule_id: str, reason: str = "No reason provided") -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
        """警告用户并计算禁言"""

        logger.debug(f"User {user_id} has been warned according to rule {rule_id}.")

        
        rules = self.rules
        if not rules:
            return None, "Rules not loaded"

        parts = rule_id.split(".")
        if len(parts) != 2:
            return None, "Invalid Rule ID"
        category, sub_id = parts

        try:
            # 2. 核心修复：直接用 ['key'] 访问并 cast，跳过 .get() 的重载推导
            # 因为你已经处理了 KeyError，所以直接索引更干净
            cat_data = cast(Dict[str, Any], rules[category])
            rule = cast(Dict[str, Any], cat_data[sub_id])
            
            # 如果走到这一步，rule_data 就是一个纯净的 Dict[str, Any]
            # Pylance 不再会有任何抱怨
            
        except (KeyError, TypeError):
            return None, f"Rule {rule_id} not found"

        with Session(self.engine) as session:
            # 2. 获取或创建用户
            user = session.get(User, user_id)
            if not user:
                user = User(zulip_id=user_id, username=f"User_{user_id}")
                session.add(user)
                logger.success(f"User {user_id} has been created automatically.")
            
            # 3. 增加警告记录
            new_warn = WarningRecord(type=rule_id, reason=reason, user_id=user_id)
            session.add(new_warn)
            session.flush() # 刷新以获取最新状态，但不提交


            # 4. 计算该类型的警告总数 (x)
            statement = select(WarningRecord).where(
                WarningRecord.user_id == user_id, 
                WarningRecord.type == rule_id
            )
            x = len(session.exec(statement).all())

            try:
                # 5. 计算禁言时长
                formula = rule.get("formula", "0")
                minutes = eval(formula, {"x": x})
                
                if minutes != 0:
                    user.is_muted = True
                    if minutes > 0:
                        # 限时禁言
                        user.mute_until = datetime.now() + timedelta(minutes=minutes)
                    else:
                        # 永久禁言 (如果是 -1)
                        user.mute_until = None
                
                session.add(user)
                session.commit()

                return {
                    "count": x,
                    "mute_mins": minutes,
                    "name": rule["name"],
                }, None
            except Exception as e:
                session.rollback()
                logger.error("Unable to warn users because " + str(e))
                return None, str(e)

    def unmute(self, user_id: int):
        """手动解除禁言"""
        
        with Session(self.engine) as session:
            user = session.get(User, user_id)
            if user and user.is_muted:
                user.is_muted = False
                user.mute_until = None
                session.add(user)
                session.commit()
                logger.success(f"User {user_id} has been unmuted.")
                return True
            return False
    def unwarn_user(self, user_id: int, rule_id: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
        """撤销用户最后一次针对该规则的警告"""
        with Session(self.engine) as session:
            # 1. 查找该用户该类型最近的一条警告 (按 ID 倒序)
            statement = select(WarningRecord).where(
                WarningRecord.user_id == user_id,
                WarningRecord.type == rule_id
            ).order_by(desc(WarningRecord.id))
            
            last_warning = session.exec(statement).first()
            
            if not last_warning:
                return None, "User has no warning records for this rule."

            # 2. 删除这条记录
            session.delete(last_warning)
            
            # 3. 重新统计剩余警告总数 (x)
            count_stmt = select(WarningRecord).where(
                WarningRecord.user_id == user_id,
                WarningRecord.type == rule_id
            )
            new_x = len(session.exec(count_stmt).all())
            
            # 4. (可选) 如果警告清零，自动解除禁言状态
            user = session.get(User, user_id)
            if user and new_x == 0:
                user.is_muted = False
                user.mute_until = None
                session.add(user)

            session.commit()
            logger.success(f"User {user_id} has been unwarned.")
            
            # 5. 获取规则名称用于返回
            cat, sub = rule_id.split(".")
            rule_name = cast(Dict[str, Any], self.rules.get(cat, {})).get(sub, {}).get("name", "Unknown")

            return {
                "count": new_x,
                "mute_mins": 0,
                "name": rule_name,
            }, None
    def set_mute(self, user_id: int, seconds: int):
        """手动设置禁言时间 (-1 为永久)"""
        with Session(self.engine) as session:
            user = session.get(User, user_id)
            if not user:
                user = User(zulip_id=user_id, username=f"User_{user_id}")
            
            user.is_muted = True
            if seconds == -1:
                user.mute_until = None # 永久禁言
            else:
                user.mute_until = datetime.now() + timedelta(seconds=seconds)
            
            session.add(user)
            session.commit()
            logger.success(f"User {user_id} has been muted for {seconds} seconds")
    def parse_time(self, time_str: str) -> Tuple[Optional[int], str]:
        """
        解析时间字符串，返回 (秒数, 标签)
        支持: 10s, 30m, 1h, 1d, always
        """
        import re
        
        ts = time_str.lower().strip()
        
        # 处理永久禁言
        if not ts or ts in ["always", "forever", "inf", "-1"]:
            return -1, "forever"
            
        # 使用正则匹配数字和单位 (s, m, h, d)
        match = re.match(r"^(\d+)\s*([smhd]?)$", ts)
        if not match:
            return None, "invalid format"
            
        val_str, unit = match.groups()
        val = int(val_str)
        
        # 默认单位是分钟 (m)，如果没写单位的话
        multipliers = {
            "s": 1,
            "m": 60,
            "": 60,   # 默认分钟
            "h": 3600,
            "d": 86400
        }
        
        seconds = val * multipliers.get(unit, 60)
        label = f"{val}{unit if unit else 'm'}"
        
        return seconds, label
    def get_all_mutes(self) -> Dict[int, float]:
        """从数据库获取所有被禁言的用户 ID 和过期时间"""
        with Session(self.engine) as session:
            # 查询所有 is_muted 为 True 的用户
            statement = select(User).where(User.is_muted == True)
            users = session.exec(statement).all()
            
            # 返回一个字典 {user_id: timestamp}
            # 如果是永久禁言，timestamp 设为 -1.0
            return {
                u.zulip_id: (u.mute_until.timestamp() if u.mute_until else -1.0)
                for u in users
            }
        
    def get_user_status(self, user_id: int) -> Dict[str, int]:
        """获取特定用户的所有警告统计"""
        with Session(self.engine) as session:
            statement = select(WarningRecord).where(WarningRecord.user_id == user_id)
            results = session.exec(statement).all()
            
            # 统计每种规则的次数
            # 返回格式如: {"1.1": 2, "2.1": 1}
            stats: Dict[str, int] = {}
            for record in results:
                stats[record.type] = stats.get(record.type, 0) + 1
            return stats