#!/bin/bash

set -ep

poetry run python webhook_server_container/utils/github_repository_settings.py
poetry run python webhook_server_container/utils/webhook.py

if [[ -z $DEVELOPMENT ]]; then
	poetry run uwsgi --disable-logging --post-buffering --master --enable-threads --http 0.0.0.0:5000 --wsgi-file webhook_server_container/app.py --callable FLASK_APP --processes 4 --threads 2
else
	poetry run python webhook_server_container/app.py
fi
