import datetime
import logging
from flask import Blueprint, Response, request
from . import config, models
from .commands import close_month

logger = logging.getLogger(__name__)

bp = Blueprint("tasks", __name__, url_prefix="/tasks")

@bp.route("/daily_topup", methods=["GET"])
def task_daily_topup():
    """Endpoint for daily top-up. Secured by TASK_SECRET."""
    if config.TASK_SECRET and request.args.get("secret") != config.TASK_SECRET:
        return Response("forbidden", status=403)
    
    try:
        cycle = models.get_or_create_cycle()
        today = datetime.datetime.now(config.LONDON_TZ).date()
        rows = models.UserCycle.query.filter(models.UserCycle.cycle_id == cycle.cycle_id).all()
        count = 0
        for uc in rows:
            if uc.last_topup_date != today:
                uc.balance = min(config.BALANCE_CAP, uc.balance + config.DAILY_TOPUP)
                uc.last_topup_date = today
                count += 1
        models.db.session.commit()
        return f"topped up {count} users for {cycle.key}"
    except Exception as e:
        models.db.session.rollback()
        logger.exception("Error in daily topup")
        return f"Error: {str(e)}", 500


@bp.route("/monthly_close", methods=["GET"])
def task_monthly_close():
    """Endpoint for month-end closure. Secured by TASK_SECRET."""
    if config.TASK_SECRET and request.args.get("secret") != config.TASK_SECRET:
        return Response("forbidden", status=403)
    
    try:
        sys_user = models.User.query.filter(models.User.slack_id == "SYSTEM").first()
        if not sys_user:
            sys_user = models.User(slack_id="SYSTEM", slack_name="system")
            models.db.session.add(sys_user)
            models.db.session.flush()
        msg = close_month(sys_user)
        models.db.session.commit()
        return msg
    except Exception as e:
        models.db.session.rollback()
        logger.exception("Error in monthly close")
        return f"Error: {str(e)}", 500
