from datetime import datetime
from typing import List, Optional
from sqlmodel import Field, SQLModel, Relationship, create_engine

# 定义用户表
class User(SQLModel, table=True):
    zulip_id: int = Field(unique=True, index=True, default=None, primary_key=True) # 给常用查询项加索引
    username: str
    is_muted: bool = Field(default=False)
    mute_until: Optional[datetime] = Field(default=None)
    warnings: List["WarningRecord"] = Relationship(back_populates="user")

class WarningRecord(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    type: str  # 对应 TOML 里的 type_a, type_b
    reason: str
    timestamp: datetime = Field(default_factory=datetime.now)
    
    # 外键关联到 User 表
    user_id: int = Field(foreign_key="user.zulip_id")
    user: User = Relationship(back_populates="warnings")


sqlite_url = "sqlite:///database.db"
engine = create_engine(sqlite_url, echo=True)

def create_db_and_tables():
    print("Attempting to create a table")
    SQLModel.metadata.create_all(engine)
    print("表创建指令已发送。")

# 3. 必须有这一步调用！
if __name__ == "__main__":
    create_db_and_tables()