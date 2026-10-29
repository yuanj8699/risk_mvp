"""业务持久化模型：目录 → 数据快照 → 风险事项 → 函件修订。

本地默认SQLite，也可配置PostgreSQL；JSON字段更新须赋新对象，不能只原地修改。
"""
from __future__ import annotations

import os
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Integer, JSON, Numeric, String, Text, UniqueConstraint, create_engine, event
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker
from sqlalchemy.types import TypeDecorator

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "data" / "risk-demo.db"
DEFAULT_DB.parent.mkdir(parents=True, exist_ok=True)
DATABASE_URL = os.getenv("RISK_DATABASE_URL", f"sqlite:///{DEFAULT_DB}")
engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}, pool_pre_ping=True)
if DATABASE_URL.startswith("sqlite"):
    @event.listens_for(engine, "connect")
    def sqlite_settings(connection, _):
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")

SessionLocal = sessionmaker(engine, expire_on_commit=False)


def now() -> datetime:
    return datetime.now(timezone.utc)


def uid() -> str:
    return uuid4().hex


class Base(DeclarativeBase):
    pass


class ExactDecimal(TypeDecorator):
    """SQLite用文本保存精确十进制，PostgreSQL使用NUMERIC。

    避免SQLite的NUMERIC affinity把金额转成浮点数；读取后统一返回Decimal。
    """

    impl = String(64)
    cache_ok = True

    def load_dialect_impl(self, dialect):
        storage = String(64) if dialect.name == "sqlite" else Numeric(24, 8)
        return dialect.type_descriptor(storage)

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        decimal_value = value if isinstance(value, Decimal) else Decimal(str(value))
        return format(decimal_value, "f") if dialect.name == "sqlite" else decimal_value

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        return value if isinstance(value, Decimal) else Decimal(str(value))


class Organization(Base):
    """机构树：parent_id表达上下级，level=3为当前监测行社。"""
    __tablename__ = "organizations"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    code: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(200))
    level: Mapped[int] = mapped_column(Integer)
    parent_id: Mapped[str | None] = mapped_column(ForeignKey("organizations.id"))
    region_id: Mapped[str | None] = mapped_column(String(64))


class Indicator(Base):
    """指标定义及单位；与具体机构、期次的指标值分开存储。"""
    __tablename__ = "indicators"
    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    unit: Mapped[str] = mapped_column(String(30))
    frequency: Mapped[str] = mapped_column(String(20))
    description: Mapped[str] = mapped_column(Text)


class Snapshot(Base):
    """不可混用的数据批次；各指标可保留自己的实际数据日期。"""
    __tablename__ = "snapshots"
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=uid)
    name: Mapped[str] = mapped_column(String(200))
    data_date: Mapped[date] = mapped_column(Date)
    source: Mapped[str] = mapped_column(String(100))
    is_demo: Mapped[bool] = mapped_column(Boolean, default=True)
    row_count: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(30), default="ready")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Observation(Base):
    """每批次、机构、指标一条事实；None是缺数，0是有效零值。"""
    __tablename__ = "observations"
    __table_args__ = (UniqueConstraint("snapshot_id", "org_id", "indicator_id", name="uq_snapshot_observation"),)
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=uid)
    snapshot_id: Mapped[str] = mapped_column(ForeignKey("snapshots.id"), index=True)
    org_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True)
    indicator_id: Mapped[str] = mapped_column(ForeignKey("indicators.id"), index=True)
    data_date: Mapped[date] = mapped_column(Date)
    value: Mapped[Decimal | None] = mapped_column(ExactDecimal(), nullable=True)
    unit: Mapped[str] = mapped_column(String(30))
    rank: Mapped[int | None] = mapped_column(Integer, nullable=True)
    population: Mapped[int | None] = mapped_column(Integer, nullable=True)
    rank_scope: Mapped[str] = mapped_column(String(100), default="")
    quality_status: Mapped[str] = mapped_column(String(30), default="valid")


class Rule(Base):
    """可配置规则及当前版本；每次运行另外保存其冻结副本。"""
    __tablename__ = "rules"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    kind: Mapped[str] = mapped_column(String(30))
    indicator_id: Mapped[str] = mapped_column(ForeignKey("indicators.id"))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    params: Mapped[dict] = mapped_column(JSON, default=dict)
    version: Mapped[int] = mapped_column(Integer, default=1)
    description: Mapped[str] = mapped_column(Text, default="")


class AnalysisRun(Base):
    """一次监测的输入身份、固定规则、检查明细与汇总。"""
    __tablename__ = "analysis_runs"
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=uid)
    run_key: Mapped[str] = mapped_column(String(64), unique=True)
    snapshot_id: Mapped[str] = mapped_column(ForeignKey("snapshots.id"))
    status: Mapped[str] = mapped_column(String(30), default="running")
    summary: Mapped[dict] = mapped_column(JSON, default=dict)
    selection: Mapped[dict] = mapped_column(JSON, default=dict)
    rule_snapshot: Mapped[list] = mapped_column(JSON, default=list)
    checks: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class RiskCase(Base):
    """同次运行按行社合并的风险事项，保存当时证据与分析。"""
    __tablename__ = "risk_cases"
    __table_args__ = (UniqueConstraint("run_id", "org_id", name="uq_case_org_run"),)
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=uid)
    run_id: Mapped[str] = mapped_column(ForeignKey("analysis_runs.id"), index=True)
    org_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"))
    org_name: Mapped[str] = mapped_column(String(200))
    region_name: Mapped[str] = mapped_column(String(200))
    data_date: Mapped[date] = mapped_column(Date)
    risk_types: Mapped[list] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(30), default="open")
    headline: Mapped[str] = mapped_column(Text)
    evidence: Mapped[list] = mapped_column(JSON, default=list)
    analysis: Mapped[dict] = mapped_column(JSON, default=dict)


class Letter(Base):
    """函件当前版本；审批同时绑定revision与正文/证据摘要。"""
    __tablename__ = "letters"
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=uid)
    case_id: Mapped[str] = mapped_column(ForeignKey("risk_cases.id"), unique=True)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    content: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(30), default="draft")
    evidence_hash: Mapped[str] = mapped_column(String(64))
    approved_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)
    approved_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    receipt: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class LetterHistory(Base):
    """每次正文修订的不可覆盖记录，供回看已审核或已模拟下发版本。"""
    __tablename__ = "letter_history"
    __table_args__ = (UniqueConstraint("letter_id", "revision", name="uq_letter_revision"),)
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=uid)
    letter_id: Mapped[str] = mapped_column(ForeignKey("letters.id"))
    revision: Mapped[int] = mapped_column(Integer)
    content: Mapped[str] = mapped_column(Text)
    evidence_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class AuditLog(Base):
    """与业务事务一起保存的操作记录，不存模型密钥或认证信息。"""
    __tablename__ = "audit_log"
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=uid)
    action: Mapped[str] = mapped_column(String(100))
    entity_id: Mapped[str] = mapped_column(String(64))
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
