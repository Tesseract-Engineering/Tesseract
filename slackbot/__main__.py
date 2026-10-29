"""
python -m slackbot [--env .env] [--meeting] [--ask EMAIL QUESTION]

The Slack app, with the Tesseract core it answers through: the same as `python -m core --slack`.
--ask answers one question as that email in the terminal, with no Slack.
"""

from core.__main__ import main

if __name__ == "__main__":
    main(prog="python -m slackbot", slack_default=True)
