from datetime import datetime
from uuid import UUID

from sqlalchemy import BigInteger, Boolean, CheckConstraint, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import ARRAY, UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class MenuItem(Base):
    __tablename__ = "menu_items"
    __table_args__ = (CheckConstraint("price_minor >= 0", name="ck_menu_items_price_nonnegative"),)

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    source_row_id: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    ingredients: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, default=list)
    allergens: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, default=list)
    dietary_tags: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, default=list, server_default=text("'{}'::text[]")
    )
    category: Mapped[str] = mapped_column(String(120), nullable=False)
    price_minor: Mapped[int] = mapped_column(Integer, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="EUR")
    available: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class RestaurantSetting(Base):
    __tablename__ = "restaurant_settings"
    key: Mapped[str] = mapped_column(String(120), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class Order(Base):
    __tablename__ = "orders"
    __table_args__ = (
        CheckConstraint("subtotal_minor >= 0", name="ck_orders_subtotal_nonnegative"),
        CheckConstraint("delivery_fee_minor >= 0", name="ck_orders_delivery_fee_nonnegative"),
        CheckConstraint("total_minor >= 0", name="ck_orders_total_nonnegative"),
        Index("ix_orders_telegram_user_id", "telegram_user_id"),
        Index("ix_orders_status", "status"),
    )
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    telegram_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    customer_name: Mapped[str] = mapped_column(String(255), nullable=False)
    phone: Mapped[str] = mapped_column(String(64), nullable=False)
    address: Mapped[str] = mapped_column(Text, nullable=False)
    requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    subtotal_minor: Mapped[int] = mapped_column(Integer, nullable=False)
    delivery_fee_minor: Mapped[int] = mapped_column(Integer, nullable=False)
    total_minor: Mapped[int] = mapped_column(Integer, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="EUR")
    status: Mapped[str] = mapped_column(String(40), nullable=False)
    confirmation_key: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    calendar_event_id: Mapped[str | None] = mapped_column(String(255))


class OrderItem(Base):
    __tablename__ = "order_items"
    __table_args__ = (
        CheckConstraint("quantity > 0", name="ck_order_items_quantity_positive"),
        CheckConstraint("unit_price_minor >= 0", name="ck_order_items_unit_price_nonnegative"),
        CheckConstraint("line_total_minor >= 0", name="ck_order_items_line_total_nonnegative"),
        Index("ix_order_items_order_id", "order_id"),
        Index("ix_order_items_menu_item_id", "menu_item_id"),
    )
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    order_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("orders.id"), nullable=False)
    menu_item_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("menu_items.id"), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    unit_price_minor: Mapped[int] = mapped_column(Integer, nullable=False)
    line_total_minor: Mapped[int] = mapped_column(Integer, nullable=False)
    name_snapshot: Mapped[str] = mapped_column(String(255), nullable=False)


class ConversationState(Base):
    __tablename__ = "conversation_state"
    thread_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class FaqSync(Base):
    __tablename__ = "faq_sync"
    source_row_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    indexed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class UnansweredQuestion(Base):
    __tablename__ = "unanswered_questions"
    __table_args__ = (Index("ix_unanswered_questions_created_at", "created_at"),)
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    sanitized_question: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    escalation_status: Mapped[str] = mapped_column(String(40), nullable=False)


class CalendarJob(Base):
    __tablename__ = "calendar_jobs"
    __table_args__ = (
        CheckConstraint("attempt_count >= 0", name="ck_calendar_jobs_attempt_count_nonnegative"),
        Index("ix_calendar_jobs_order_id", "order_id"),
        Index("ix_calendar_jobs_status_next_attempt", "status", "next_attempt_at"),
    )
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    order_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("orders.id"), nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(40), nullable=False)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
