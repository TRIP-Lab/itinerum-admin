#!/usr/bin/env python
# -*- coding: utf-8 -*-
# Kyle Fitzsimmons, 2017
import codecs
import csv
from datetime import date, datetime, timedelta
from flask import (Blueprint, current_app, jsonify, make_response,
                   render_template, request, Response)
from functools import wraps
import io
import json
import os
import pytz
import time

from admin.database import Database
from utils.responses import Success, Error

blueprint = Blueprint('control_panel', __name__)
database = Database()
server_tz = pytz.timezone('America/Montreal')


### THIS SHOULD BE CHANGED TO A MORE ROBUST SYSTEM
# Left as-is for time being as panel intended for single-user
# server adminstrator
def check_auth(username, password):
    return username == current_app.config['ADMIN_USER'] and \
           password == current_app.config['ADMIN_PASSWORD']


# HTTP Basic Auth decorator
def requires_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        auth = request.authorization
        if not auth or not check_auth(auth.username, auth.password):
            message = ('Could not verify your access level for that URL.\n'
                       'You have to login with proper credentials.')
            headers = {'WWW-Authenticate': 'Basic realm="Login Required"'}
            return Response(message, 401, headers)
        return f(*args, **kwargs)
    return decorated


@blueprint.route('/')
@requires_auth
def home():
    page_data = {
        'title': 'Overview - Itinerum Control Panel',
        'surveys': database.survey_admin.names()
    }
    page_data.update(database.token.counts())
    return render_template('control_panel.index.html', **page_data)


@blueprint.route('/health')
def ecs_health_check():
    response = {'status': 0}
    return make_response(jsonify(response))


@blueprint.route('/export')
@requires_auth
def export_csv():
    survey = request.args.get('survey')
    yesterday = date.today() - timedelta(days=1)
    start = yesterday.strftime('%Y-%m-%d 00:00:00')
    start_dt = datetime.strptime(start, '%Y-%m-%d %H:%M:%S').replace(tzinfo=server_tz)
    end = yesterday.strftime('%Y-%m-%d 23:59:59')
    end_dt = datetime.strptime(end, '%Y-%m-%d %H:%M:%S').replace(tzinfo=server_tz)

    summary = database.survey_admin.generate_summary(survey_name=survey,
                                                     start=start_dt,
                                                     end=end_dt)

    summary_rows = [
        [survey.encode('utf-8')],
        [yesterday.strftime('%d-%m-%Y')],
        [],
        ['New users', 'Active users', 'Points', 'Prompts'],
        [summary['new_users'], summary['active_users'], summary['num_points'], summary['num_prompts']]
    ]

    filestream = io.BytesIO()
    filestream.write(codecs.BOM_UTF8)
    writer = csv.writer(filestream)
    writer.writerows(summary_rows)
    csv_data = filestream.getvalue().strip('\r\n')

    csv_fn = '{survey}-{date}.csv'.format(survey=survey.encode('utf-8'), date=yesterday)
    headers = {
        'Content-disposition': 'attachment; filename={fn}'.format(fn=csv_fn)
    }
    response = Response(csv_data,
                        mimetype='text/csv',
                        headers=headers)
    response.content_type = 'text/csv'
    return response


@blueprint.route('/new-survey')
@requires_auth
def new_survey_panel():
    page_data = {
        'title': 'New Survey Tokens - Itinerum Control Panel',
        'new_survey_tokens': database.token.new_survey.get_recent(10)
    }
    return render_template('control_panel.new_survey.html', **page_data)


@blueprint.route('/new-survey/schema', methods=['POST'])
@requires_auth
def upload_survey_schema_json():
    if not request.json.get('surveyName'):
        return Error(status_code=400,
                     headers={'Location': '/new-survey/schema'},
                     resource_type='NewSurveySchema',
                     errors=['A unique survey name must be provided.'])
    errors = None
    if request.json.get('schema'):
        error = database.survey_admin.create_from_schema(survey_name=request.json['surveyName'],
                                                         admin_email=request.json['schema']['adminEmail'],
                                                         admin_password=request.json['schema']['adminPassword'],
                                                         language=request.json['schema']['language'],
                                                         survey_questions=request.json['schema']['surveyQuestions'],
                                                         survey_prompts=request.json['schema']['surveyPrompts'])
        if not error:
            return Success(status_code=201,
                           headers={'Location': '/new-survey/schema'},
                           resource_type='NewSurveySchema',
                           body={})
        errors = [error]

    if not errors:
        errors = ['New survey schema could not be uploaded.']
    return Error(status_code=400,
                 headers={'Location': '/new-survey/schema'},
                 resource_type='NewSurveySchema',
                 errors=errors)


@blueprint.route('/new-survey/token', methods=['POST'])
@requires_auth
def generate_new_survey_token():
    database.token.new_survey.create()
    response = {
        'recent_tokens': [],
        'message': 'New survey sign-up token successfully created.'
    }

    # format the same as jinja2 renders the template
    for token in database.token.new_survey.get_recent(10):
        response['recent_tokens'].append({
            'token': token.token,
            'created_at': token.created_at.strftime('%Y-%m-%d %H:%M:%S'),
            'active': str(token.active),
            'usages': token.usages
        })

    return Success(status_code=201,
                   headers={'Location': '/new-survey/token'},
                   resource_type='NewSurveyToken',
                   body=response)


@blueprint.route('/researcher-invites')
@requires_auth
def researcher_invites_panel():
    page_data = {
        'title': 'Researcher Invite Tokens - Itinerum Control Panel',
        'researcher_tokens': []
    }
    for token in database.token.researcher_invite.get_active():
        page_data['researcher_tokens'].append({
            'token': token.token,
            'survey_id': token.survey_id,
            'pretty_name': token.survey.pretty_name,
            'admin_email': database.survey_admin.get_admin_email(token.survey),
            'created_at': token.created_at.replace(microsecond=0),
            'usages': token.usages
        })

    return render_template('control_panel.researcher_invites.html', **page_data)


@blueprint.route('/account-recovery', methods=['GET', 'POST'])
@requires_auth
def account_recovery_panel():
    if request.method == 'GET':
        response = {'title': 'Account Recovery Tokens - Itinerum Control Panel'}
        return render_template('control_panel.account_recovery.html', **response)
    elif request.method == 'POST':
        page = int(request.form.get('pageNum', 1))
        email = request.form.get('email', '').strip()
        tokens = database.token.account_recovery.latest(email=email)

        pagination = tokens.paginate(page=page, per_page=10)
        response = {
            'latest_tokens': [],
            'pages': pagination.pages,
            'per_page': pagination.per_page
        }
        for token in pagination.items:
            used_at = None
            if not token.active:
                used_at = (token.modified_at.astimezone(server_tz)
                                            .replace(microsecond=0)
                                            .strftime('%Y-%m-%d %H:%M:%S'))
            response['latest_tokens'].append({
                'id': token.id,
                'survey_id': token.web_user.survey_id,
                'pretty_name': token.web_user.survey.pretty_name,
                'email': token.web_user.email,
                'token': token.token,
                'used_at': used_at
            })
        return make_response(jsonify(response))


@blueprint.route('/account-recovery/deactivate', methods=['POST'])
@requires_auth
def disable_account_recovery_token():
    token_id = request.form['tokenId']
    token = database.token.account_recovery.disable(token_id)
    response = {
        'message': 'WebUserResetPasswordToken {id} disabled.'.format(id=token.id)
    }
    return Success(status_code=201,
                   headers={'Location': '/new-survey/deactivate'},
                   resource_type='DeactivateNewSurveyToken',
                   body=response)


@blueprint.route('/recent-activity', methods=['GET'])
@requires_auth
def recent_activity_panel():
    response = {}
    return render_template('control_panel.recent_activity.html', **response)


@blueprint.route('/recent-activity/data', methods=['POST'])
@requires_auth
def recent_activity():
    start = datetime.now(pytz.utc) - timedelta(hours=1)
    end = datetime.now(pytz.utc)

    points_feature = {
        'type': 'MultiPoint',
        'coordinates': [],
        'properties': {
            'timestamps': [],
            'mobile_ids': []
        }
    }

    newest = 0
    oldest = 999999999999999
    for c in database.activity.coordinates_by_times(start, end):
        points_feature['coordinates'].append([float(c.longitude), float(c.latitude)])
        ts = int(time.mktime(c.timestamp.utctimetuple()))
        points_feature['properties']['timestamps'].append(ts)
        points_feature['properties']['mobile_ids'].append(c.mobile_id)

        if ts > newest:
            newest = ts
        if ts < oldest:
            oldest = ts
    points_feature['properties']['start'] = oldest
    points_feature['properties']['end'] = newest

    # prompts_feature = {
    #     'type': 'Feature',
    #     'geometry': {
    #         'type': 'MultiPoint',
    #         'coordinates': []
    #     },
    #     'properties': {'type': 'prompts'}
    # }
    # for c in database.activity.prompts_by_times(start, end):
    #     prompts_feature['geometry']['coordinates'].append([float(c.longitude), float(c.latitude)])

    # response = {
    #     'geojson': {
    #         'type': 'FeatureCollection',
    #         'features': [points_feature]
    #     }
    # }

    return make_response(jsonify(points_feature))


@blueprint.route('/recent-activity/data/<mobile_id>', methods=['POST'])
@requires_auth
def recent_user_activity(mobile_id):
    points_feature = {
        'type': 'MultiPoint',
        'coordinates': [],
        'properties': {
            'uuid': None,
            'survey_id': None,
            'survey_name': None,
            'timestamps': [],
            'start': None,
            'end': None
        }
    }

    user = database.user_lookup.get(mobile_id)
    survey = user.survey_response.one_or_none()
    if user:
        points_feature['properties']['uuid'] = user.uuid
        points_feature['properties']['survey_id'] = user.survey_id
        points_feature['properties']['survey_name'] = user.survey.pretty_name

        if survey:
            points_feature['properties']['email'] = survey.response.get('Email')

        points = database.activity.coordinates_by_mobile_id(mobile_id)
        for p in points:
            ts = int(time.mktime(p.timestamp.utctimetuple()))
            points_feature['properties']['timestamps'].append(ts)
            points_feature['coordinates'].append([float(p.longitude), float(p.latitude)])

    points_feature['properties']['start'] = points_feature['properties']['timestamps'][0]
    points_feature['properties']['end'] = points_feature['properties']['timestamps'][-1]
    return make_response(jsonify(points_feature))

@blueprint.route('/recent-activity/export', methods=['GET'])
@requires_auth
def recent_user_activity_geojson():
    mobile_id = request.args.get('mobileuser')
    user_geojson = {
        'type': 'FeatureCollection',
        'features': []
    }

    points = database.activity.coordinates_by_mobile_id(mobile_id)
    for p in points:
        feature = {
            'type': 'Feature',
            'geometry': {
                'type': 'Point',
                'coordinates': [float(p.longitude), float(p.latitude)]
            },
            'properties': {
                'id': p.id,
                'timestamp': p.timestamp.isoformat(),
                'h_accuracy': float(p.h_accuracy),
                'v_accuracy': float(p.v_accuracy),
                'speed': float(p.speed)

            }
        }
        user_geojson['features'].append(feature)
    return Response(json.dumps(user_geojson, indent=4),
                    mimetype='text/json',
                    headers={'Content-disposition': 'attachment; filename=mobileid-{}.geojson'.format(mobile_id)})

@blueprint.route('/user-lookup', methods=['GET', 'POST'])
@requires_auth
def user_lookup_panel():
    if request.method == 'GET':
        response = {
            'title': 'User Lookup - Itinerum Control Panel',
            'surveys': database.survey_admin.names()
        }
        return render_template('control_panel.user_lookup.html', **response)
    elif request.method == 'POST':
        uuid_or_email = request.form.get('uuidOrEmail', '').strip()
        survey_name = request.form.get('surveyName', '').strip()
        page = int(request.form.get('pageNum', 1))

        if uuid_or_email:
            users = database.user_lookup.paginate(survey_name=survey_name,
                                                  page=page,
                                                  uuid_or_email=uuid_or_email)
        else:
            users = database.user_lookup.paginate(survey_name=survey_name, page=page)

        response = {
            'users': [],
            'pages': users.pages,
            'per_page': users.per_page
        }
        for user in users.items:
            # format timestamps in Montreal local time
            created_at = user.created_at.astimezone(server_tz).strftime('%Y-%m-%d %H:%M:%S')
            last_mobile_update = None
            if user.last_coordinate:
                last_mobile_update = (user.last_coordinate.timestamp
                                                          .astimezone(server_tz)
                                                          .strftime('%Y-%m-%d %H:%M:%S'))
            response['users'].append({
                'id': user.id,
                'created_at': created_at,
                'email': user.email,
                'last_coordinate': last_mobile_update,
                'survey_administrator': user.admin_user.email,
                'survey_language': user.survey.language,
                'survey': user.survey.pretty_name,
                'survey_id': user.survey_id,
                'uuid': user.uuid
            })
        return make_response(jsonify(response))


@blueprint.route('/logout')
def logout():
    message = 'You have successfully logged out. Click <a href="./">here</a> to return.'
    return Response(message, 401)