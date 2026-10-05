"""Natural-calendar material windows, using the configured local date."""

from datetime import date, timedelta

from config.schedules import WorkflowSchedule


def due(schedule: WorkflowSchedule, today: date) -> bool:
    if not schedule.enabled:
        return False
    if schedule.schedule_type == "interval_days":
        anchor = date.fromisoformat(str(schedule.options["anchor_date"]))
        return today >= anchor and (today - anchor).days % int(schedule.options["every_days"]) == 0
    if schedule.schedule_type == "weekly":
        return today.weekday() == ["mon", "tue", "wed", "thu", "fri", "sat", "sun"].index(schedule.options["weekday"])
    if schedule.schedule_type == "monthly":
        return today.day == int(schedule.options["day"])
    return True


def material_window(schedule: WorkflowSchedule, today: date) -> tuple[date, date]:
    options = schedule.options
    period = options.get("material_period")
    if period == "previous_month":
        end = today.replace(day=1) - timedelta(days=1)
        return end.replace(day=1), end
    if period == "previous_week":
        start = today - timedelta(days=today.weekday() + 7)
        return start, start + timedelta(days=6)
    if period not in {None, "last_complete_days"}:
        raise ValueError(f"不支持的材料周期：{period}")
    days = int(options.get("material_days", 3))
    if days <= 0:
        raise ValueError("材料天数必须为正整数")
    end = today - timedelta(days=1)
    return end - timedelta(days=days - 1), end


def latest_due(schedule: WorkflowSchedule, today: date) -> date | None:
    if schedule.schedule_type == "interval_days":
        anchor = date.fromisoformat(str(schedule.options["anchor_date"]))
        if today < anchor:
            return None
        return today - timedelta(days=(today - anchor).days % int(schedule.options["every_days"]))
    if schedule.schedule_type == "weekly":
        weekday = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"].index(schedule.options["weekday"])
        return today - timedelta(days=(today.weekday() - weekday) % 7)
    if schedule.schedule_type == "monthly":
        day = int(schedule.options["day"])
        candidate = today.replace(day=day)
        if candidate > today:
            candidate = (today.replace(day=1) - timedelta(days=1)).replace(day=day)
        return candidate
    return today
