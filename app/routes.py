import os
import json
import logging
import shlex
from flask import Blueprint, request, Response
from sqlalchemy import text

from . import config, models
from .commands import commands
from .exceptions import PredictionsError

logger = logging.getLogger(__name__)

bp = Blueprint("main", __name__)

@bp.route("/", methods=["POST"])
def handle_request():
    """
    Main Slack slash-command handler.
    
    Parses Slack request, routes to command function, and returns JSON response.
    """
    try:
        # Verify Slack token
        if request.form.get("token") != os.environ.get("SLACK_TOKEN"):
            return Response(
                json.dumps({"response_type": "ephemeral", "text": "Invalid token"}),
                mimetype="application/json",
                status=401,
            )
        
        # Parse args
        text_content = request.form.get("text", "").strip()
        args = shlex.split(text_content) if text_content else ["help"]
        slack_user_id = request.form.get("user_id")
        slack_user_name = request.form.get("user_name")
        
        logger.info(f"Command received: text='{text_content}', args={args}, user={slack_user_id}")
        
        if not slack_user_id:
            return Response(
                json.dumps({"response_type": "ephemeral", "text": "Missing user information"}),
                mimetype="application/json",
                status=400,
            )
        
        # Get or create user
        user = models.get_or_create_user(slack_user_id, slack_user_name)
        
        # Route command
        command_str = "help"
        if args and args[0] in commands:
            command_str = args[0]
            args = args[1:]
        elif len(args) >= 3:
            # Shorthand: /predict <market> <outcome> <spend>
            command_str = "buy"
        
        logger.info(f"Executing command: {command_str} with args: {args}")
        
        # Get command entry
        entry = commands.get(command_str)
        if not entry:
            raise PredictionsError("unknown command")
        
        selected_command = entry["fn"]
        expected_params = entry.get("params", [])
        has_varargs = entry.get("has_varargs", False)
        
        logger.info(f"Expected args: {expected_params}, has_varargs: {has_varargs}, received args: {args}")
        
        # Validate argument count
        if len(args) < len(expected_params):
            if expected_params:
                usage_str = f"usage is {command_str} {' '.join(f'<{p}>' for p in expected_params)}"
            else:
                usage_str = f"usage is {command_str}"
            raise PredictionsError(usage_str)
        
        # Truncate extra args only if function doesn't accept *args
        if not has_varargs:
            args = args[: len(expected_params)]
        
        # Execute
        response = selected_command(user, *args)
        models.db.session.commit()
        
        return Response(
            json.dumps({"response_type": "in_channel", "text": response}),
            mimetype="application/json",
        )
    
    except PredictionsError as e:
        models.db.session.rollback()
        logger.info(f"Predictions error: {e}")
        return Response(
            json.dumps({"response_type": "ephemeral", "text": f"Error: {str(e)}"}),
            mimetype="application/json",
        )
    except Exception as e:
        models.db.session.rollback()
        logger.exception("Error in handle_request")
        return Response(
            json.dumps({"response_type": "ephemeral", "text": "Internal error occurred"}),
            mimetype="application/json",
            status=500,
        )


@bp.route("/health", methods=["GET"])
def health_check():
    """Health check endpoint."""
    try:
        models.db.session.execute(text("SELECT 1"))
        return "OK"
    except Exception as e:
        logger.exception("Health check failed")
        return f"Error: {str(e)}", 500
