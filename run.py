#!/usr/bin/env python3

# coding=utf-8
# pylint: disable=broad-except,unused-argument,line-too-long, unused-variable
# Copyright (c) 2016-2018, F5 Networks, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
import os
import sys
import json
import logging
import datetime
import time
import requests
import concurrent.futures
import threading
import shutil
import random

LOG = logging.getLogger('ibmcloud_test_harness_run')
LOG.setLevel(logging.DEBUG)
FORMATTER = logging.Formatter(
    '%(asctime)s - %(name)s - %(levelname)s - %(message)s')
LOGSTREAM = logging.StreamHandler(sys.stdout)
LOGSTREAM.setFormatter(FORMATTER)
LOG.addHandler(LOGSTREAM)

SCRIPT_DIR = os.path.dirname(os.path.realpath(__file__))

QUEUE_DIR = "%s/queued_tests" % SCRIPT_DIR
RUNNING_DIR = "%s/running_tests" % SCRIPT_DIR
COMPLETE_DIR = "%s/completed_tests" % SCRIPT_DIR
ERRORED_DIR = "%s/errored_tests" % SCRIPT_DIR

AUTH_ENDPOINT = 'https://iam.cloud.ibm.com/identity/token'

SESSION_TOKEN = None
REFRESH_TOKEN = None
SESSION_TIMESTAMP = 0

REQUEST_RETRIES = 10
REQUEST_DELAY = 10

WORKSPACE_CREATE_TIMEOUT = 300
WORKSPACE_PLAN_TIMEOUT = 300
WORKSPACE_APPLY_TIMEOUT = 300
WORKSPACE_DESTROY_TIMEOUT = 300
WORKSPACE_DELETE_TIMEOUT = 300
WORKSPACE_RETRY_INTERVAL = 10
WORKSPACE_STATUS_POLL_INTERVAL = 10

REPORT_REQUEST_INTERVAL = 30

CONFIG_FILE = "%s/runners-config.json" % SCRIPT_DIR
CONFIG = {}

MY_PID = None


def check_pid(pid):
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    else:
        return True


def start_report(test_id, start_data):
    headers = {
        'Content-Type': 'application/json'
    }
    requests.post("%s/start/%s" % (CONFIG['report_service_base_url'], test_id),
                  headers=headers, data=json.dumps(start_data))


def update_report(test_id, update_data):
    headers = {
        'Content-Type': 'application/json'
    }
    requests.put("%s/report/%s" % (CONFIG['report_service_base_url'], test_id),
                 headers=headers, data=json.dumps(update_data))


def stop_report(test_id, results):
    headers = {
        'Content-Type': 'application/json'
    }
    requests.post("%s/stop/%s" % (CONFIG['report_service_base_url'], test_id),
                  headers=headers, data=json.dumps(results))


def poll_report(test_id):
    end_time = time.time() + int(CONFIG['test_timeout'])
    while (end_time - time.time()) > 0:
        base_url = CONFIG['report_service_base_url']
        headers = {
            'Content-Type': 'application/json'
        }
        response = requests.get("%s/report/%s" %
                                (base_url, test_id), headers=headers)
        if response.status_code < 400:
            data = response.json()
            if data['duration'] > 0:
                LOG.info('test run %s completed', test_id)
                return data
        seconds_left = int(end_time - time.time())
        LOG.debug('test_id: %s with %s seconds left',
                  test_id, str(seconds_left))
        time.sleep(REPORT_REQUEST_INTERVAL)
    return None


def get_iam_token():
    global SESSION_TOKEN, REFRESH_TOKEN, SESSION_TIMESTAMP
    if SESSION_TIMESTAMP > 0 and ((SESSION_TIMESTAMP - int(time.time())) > 60):
        return SESSION_TOKEN
    headers = {
        "Accept": "application/json",
        "Authorization": "Basic Yng6Yng=",
        "Content-Type": "application/x-www-form-urlencoded"
    }
    data = "apikey=%s&grant_type=urn:ibm:params:oauth:grant-type:apikey" % CONFIG['api_key']
    try:
        response = requests.post(AUTH_ENDPOINT, headers=headers, data=data)
        if response.status_code < 300:
            response_json = response.json()
            SESSION_TIMESTAMP = (int(time.time()) +
                                 int(response_json['expires_in']))
            SESSION_TOKEN = response_json['access_token']
            REFRESH_TOKEN = response_json['refresh_token']
            return SESSION_TOKEN
        else:
            LOG.error('could not get an access token %d - %s',
                      response.status_code, response.content)
            SESSION_TIMESTAMP = 0
            return None
    except Exception as te:
        LOG.error('exception while getting IAM token %s - %s',
                  te, response.status_code)
        SESSION_TIMESTAMP = 0
        return None


def get_refresh_token():
    now = int(time.time())
    if SESSION_TIMESTAMP > 0 and ((SESSION_TIMESTAMP - int(time.time())) > 60):
        return REFRESH_TOKEN
    else:
        get_iam_token()
        return REFRESH_TOKEN


def poll_workspace_until(url, statuses, timeout):
    w_id = os.path.basename(url)
    LOG.debug('polling workspace %s for %d seconds', w_id, timeout)
    end_time = time.time() + timeout
    while (end_time - time.time()) > 0:
        try:
            token = get_iam_token()
            refresh_token = get_refresh_token()
            headers = {
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Authorization": "Bearer %s" % token,
                "refresh_token": refresh_token
            }
            response = requests.get(url, headers=headers)
            while response.status_code == 429:
                LOG.debug('exceeded throttling limit.. retrying')
                time.sleep(WORKSPACE_RETRY_INTERVAL)
                response = requests.post(url, headers=headers)
                LOG.info('polling workspace returned %d for %s',
                         response.status_code, url)
            if response.status_code < 400:
                response_json = response.json()
                if response_json['status'].lower() in statuses:
                    LOG.info('polling workspace %s return status %s',
                             w_id, response_json['status'])
                    return response_json['status']
                else:
                    LOG.debug('polling workspace %s interim status %s',
                              w_id, response_json['status'])

        except Exception as pe:
            LOG.error('exception polling workspace %s - %s', w_id, pe)
            return False
        time.sleep(WORKSPACE_STATUS_POLL_INTERVAL)
    return False


def create_workspace(test_id, url, data):
    LOG.info('creating Schematic workspace for %s', test_id)
    token = get_iam_token()
    refresh_token = get_refresh_token()
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Authorization": "Bearer %s" % token,
        "refresh_token": refresh_token
    }
    response = requests.post(url, headers=headers, data=json.dumps(data))
    LOG.info('workspace create returned %d for %s',
             response.status_code, test_id)
    while response.status_code == 429:
        LOG.debug('exceeded throttling limit.. retrying')
        time.sleep(WORKSPACE_RETRY_INTERVAL)
        response = requests.post(url, headers=headers)
        LOG.info('workspace create returned %d for %s',
                 response.status_code, test_id)
    if response.status_code < 300:
        workspace_id = response.json()['id']
        status_url = "%s/%s" % (url, workspace_id)
        LOG.info('polling for workspace create to complete for %s', test_id)
        status_returned = poll_workspace_until(
            status_url, ['inactive', 'failed', 'template error'], WORKSPACE_CREATE_TIMEOUT)
        if status_returned:
            if status_returned.lower() == 'inactive':
                return (workspace_id, status_returned)
            else:
                LOG.error('workspace %s returned status %s - failed to create',
                          workspace_id, status_returned)
                return (None, "created workspace %s returned status %s" % (workspace_id, status_returned))
        else:
            return (None, "create reqeust to %s timed-out" % url)
        return (None, 'could not create workspace for %s - %d - %s' % (test_id, response.status_code, response.text))


def do_plan(test_id, url, workspace_id):
    LOG.info('planning Schematic workspace for %s', test_id)
    plan_url = "%s/%s/plan" % (url, workspace_id)
    token = get_iam_token()
    refresh_token = get_refresh_token()
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Authorization": "Bearer %s" % token,
        "refresh_token": refresh_token
    }
    response = requests.post(plan_url, headers=headers)
    LOG.info('workspace plan returned %d for %s',
             response.status_code, test_id)
    while response.status_code == 409:
        LOG.debug('workspace locked.. retrying')
        time.sleep(WORKSPACE_RETRY_INTERVAL)
        response = requests.post(plan_url, headers=headers)
        LOG.info('workspace plan returned %d for %s',
                 response.status_code, test_id)
    while response.status_code == 429:
        LOG.debug('exceeded throttling limit.. retrying')
        time.sleep(WORKSPACE_RETRY_INTERVAL)
        response = requests.post(plan_url, headers=headers)
        LOG.info('workspace plan returned %d for %s',
                 response.status_code, test_id)
    if response.status_code < 300:
        activity_id = response.json()['activityid']
        status_url = "%s/%s" % (url, workspace_id)
        LOG.info('polling for workspace plan to initiate for %s', test_id)
        status_returned = poll_workspace_until(
            status_url, ['inprogress', 'failed'], WORKSPACE_PLAN_TIMEOUT
        )
        if status_returned:
            if status_returned.lower() == 'failed':
                status_message = "workspace %s plan with activity_id %s returned status %s" % (
                    workspace_id, activity_id, status_returned)
                log = get_log(url, workspace_id, activity_id)
                update_log = {"terraform_plan_log": log}
                update_report(test_id, update_log)
                return (None, status_message)
            else:
                LOG.info('polling for workspace plan to complete for %s', test_id)
                status_returned = poll_workspace_until(
                    status_url, ['inactive', 'failed'], WORKSPACE_PLAN_TIMEOUT)
                if status_returned:
                    if status_returned.lower() == 'inactive':
                        return (activity_id, status_returned)
                    else:
                        status_message = "workspace %s plan with activity_id %s returned status %s" % (
                            workspace_id, activity_id, status_returned)
                        log = get_log(url, workspace_id, activity_id)
                        update_log = {"terraform_plan_log": log}
                        update_report(test_id, update_log)
                        return (None, status_message)
                else:
                    status_message = "workspace %s plan timed-out" % plan_url
                    return (None, status_message)
    else:
        return (None, 'could not plan workspace for %s - %d - %s' %
                (test_id, response.status_code, response.text))


def do_apply(test_id, url, workspace_id):
    LOG.info('applying Schematic workspace for %s', test_id)
    apply_url = "%s/%s/apply" % (url, workspace_id)
    token = get_iam_token()
    refresh_token = get_refresh_token()
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Authorization": "Bearer %s" % token,
        "refresh_token": refresh_token
    }
    response = requests.put(apply_url, headers=headers)
    LOG.info('workspace apply returned %d for %s',
             response.status_code, test_id)
    while response.status_code == 409:
        if response.status_code == 409:
            LOG.debug('workspace locked.. retrying')
        time.sleep(WORKSPACE_RETRY_INTERVAL)
        response = requests.put(apply_url, headers=headers)
        LOG.info('workspace apply returned %d for %s',
                 response.status_code, test_id)
    while response.status_code == 429:
        LOG.debug('exceeded throttling limit.. retrying')
        time.sleep(WORKSPACE_RETRY_INTERVAL)
        response = requests.put(apply_url, headers=headers)
        LOG.info('workspace apply returned %d for %s',
                 response.status_code, test_id)
    if response.status_code < 300:
        activity_id = response.json()['activityid']
        status_url = "%s/%s" % (url, workspace_id)
        LOG.info('polling for workspace apply to complete for %s', test_id)
        status_returned = poll_workspace_until(
            status_url, ['active', 'failed'], WORKSPACE_APPLY_TIMEOUT)
        if status_returned:
            if status_returned.lower() == 'active':
                return (activity_id, status_returned)
            else:
                status_message = "workspace %s apply with activity_id %s returned status %s" % (
                    workspace_id, activity_id, status_returned)
                log = get_log(url, workspace_id, activity_id)
                update_log = {"terraform_apply_log": log}
                update_report(test_id, update_log)
                return (None, status_message)
        else:
            status_message = "workspace %s apply timed-out" % apply_url
            return (None, status_message)
    else:
        LOG.error('could not apply workspace for %s - %d - %s',
                  test_id, response.status_code, response.text)
        return (None, 'could not apply workspace for %s - %d - %s' %
                (test_id, response.status_code, response.text))


def do_destroy(test_id, url, workspace_id):
    LOG.info('destroying Schematic workspace for %s', test_id)
    destroy_url = "%s/%s/destroy" % (url, workspace_id)
    token = get_iam_token()
    refresh_token = get_refresh_token()
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Authorization": "Bearer %s" % token,
        "refresh_token": refresh_token
    }
    response = requests.put(destroy_url, headers=headers)
    LOG.info('workspace destroy returned %d for %s',
             response.status_code, test_id)
    while response.status_code == 409:
        if response.status_code == 409:
            LOG.debug('workspace locked.. retrying')
        time.sleep(WORKSPACE_RETRY_INTERVAL)
        response = requests.put(destroy_url, headers=headers)
        LOG.info('workspace apply returned %d for %s',
                 response.status_code, test_id)
    while response.status_code == 429:
        LOG.debug('exceeded throttling limit.. retrying')
        time.sleep(WORKSPACE_RETRY_INTERVAL)
        response = requests.put(destroy_url, headers=headers)
        LOG.info('workspace apply returned %d for %s',
                 response.status_code, test_id)
    if response.status_code < 300:
        activity_id = response.json()['activityid']
        status_url = "%s/%s" % (url, workspace_id)
        LOG.info('polling for workspace apply to complete for %s', test_id)
        status_returned = poll_workspace_until(
            status_url, ['inactive', 'failed'], WORKSPACE_DESTROY_TIMEOUT)
        if status_returned:
            if status_returned.lower() == 'inactive':
                return (activity_id, status_returned)
            else:
                status_message = "workspace %s destory with activity_id %s returned status %s" % (
                    workspace_id, activity_id, status_returned)
                log = get_log(url, workspace_id, activity_id)
                update_log = {"terraform_destroy_log": log}
                update_report(test_id, update_log)
                return (None, status_message)
        else:
            status_message = "workspace %s apply timed-out" % destroy_url
            return (None, status_message)
    else:
        LOG.error('could not apply workspace for %s - %d - %s',
                  test_id, response.status_code, response.text)
        return (None, 'could not apply workspace for %s - %d - %s' %
                (test_id, response.status_code, response.text))


def delete_workspace(url, workspace_id):
    LOG.info('deleting Schematic workspace for %s', workspace_id)
    status_url = "%s/%s" % (url, workspace_id)
    status_returned = poll_workspace_until(
        status_url, ['inactive', 'active', 'failed', 'draft', 'templateerror'], WORKSPACE_DELETE_TIMEOUT)
    delete_url = "%s/%s" % (url, workspace_id)
    if status_returned.lower() in ['active', 'draft', 'failed']:
        delete_url = "%s/%s/?destroyResources=true" % (url, workspace_id)
    token = get_iam_token()
    refresh_token = get_refresh_token()
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Authorization": "Bearer %s" % token,
        "refresh_token": refresh_token
    }
    response = requests.delete(delete_url, headers=headers)
    LOG.info('workspace delete returned %d for %s',
             response.status_code, workspace_id)
    while response.status_code == 409:
        LOG.debug('workspace locked.. retrying')
        time.sleep(WORKSPACE_RETRY_INTERVAL)
        response = requests.delete(delete_url, headers=headers)
        LOG.info('workspace delete returned %d for %s',
                 response.status_code, workspace_id)
    while response.status_code == 429:
        LOG.debug('exceeded throttling limit.. retrying')
        time.sleep(WORKSPACE_RETRY_INTERVAL)
        response = requests.delete(delete_url, headers=headers)
        LOG.info('workspace delete returned %d for %s',
                 response.status_code, delete_url)
    if response.status_code < 300:
        return True
    else:
        LOG.error('could not destroy workspace %s - %s - %s',
                  workspace_id, response.status_code, response.text)


def get_log(url, workspace_id, activity_id):
    log_url = "%s/%s/actions/%s/logs" % (url, workspace_id, activity_id)
    token = get_iam_token()
    refresh_token = get_refresh_token()
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Authorization": "Bearer %s" % token,
        "refresh_token": refresh_token
    }
    response = requests.get(log_url, headers=headers)
    LOG.info('activity get log returned %d for %s',
             response.status_code, activity_id)
    while response.status_code == 409:
        LOG.debug('workspace locked.. retrying')
        time.sleep(WORKSPACE_RETRY_INTERVAL)
        response = requests.get(log_url, headers=headers)
        LOG.info('activity get log returned %d for %s',
                 response.status_code, activity_id)
    while response.status_code == 429:
        LOG.debug('exceeded throttling limit.. retrying')
        time.sleep(WORKSPACE_RETRY_INTERVAL)
        response = requests.get(log_url, headers=headers)
        LOG.info('activity get log returned %d for %s',
                 response.status_code, activity_id)
    if response.status_code < 300:
        log_json = response.json()
        log_url = log_json['templates'][0]['log_url']
        response = requests.get(log_url, headers=headers)
        return response.text
    else:
        return None


def run_test(test_path):
    (zone, image, ttype, test_dir) = initialize_test_dir(test_path)
    test_id = os.path.basename(test_dir)
    LOG.info('running test %s' % test_id)
    url = None
    with open(os.path.join(test_dir, 'service_endpoint.url'), 'r') as eu:
        url = eu.read()
    data = {}
    with open(os.path.join(test_dir, 'create_data.json')) as cdf:
        data = json.load(cdf)
    start_data = {
        'zone': zone,
        'image_name': image,
        'type': ttype
    }
    create_start = datetime.datetime.utcnow().timestamp()
    (workspace_id, create_status) = create_workspace(test_id, url, data)
    if workspace_id:
        # with open(os.path.join(test_path, 'STATUS'), 'w') as status_file:
        #    status_file.write('CREATED')
        create_stop = datetime.datetime.utcnow().timestamp()
        start_report(test_id, start_data)
        update_report(test_id, {
            'workspace_create_start': create_start,
            'workspace_create_stop': create_stop,
            'workspace_create_duration': int(create_stop - create_start),
            'workspace_create_result_code': 0,
            'workspace_id': workspace_id,
            'workspace_create_status': create_status,
            'terraform_result_code': -1
        })
        plan_start = datetime.datetime.utcnow().timestamp()
        (plan_activity_id, plan_status) = do_plan(test_id, url, workspace_id)
        if plan_activity_id:
            #with open(os.path.join(test_path, 'STATUS'), 'w') as status_file:
            #    status_file.write('PLANNED')
            plan_stop = datetime.datetime.utcnow().timestamp()
            update_report(test_id, {
                'terraform_plan_start': plan_start,
                'terraform_plan_stop': plan_stop,
                'terraform_plan_duration': int(plan_stop - plan_start),
                'terraform_plan_result_code': 0,
                'terraform_plan_status': plan_status
            })
            log = get_log(url, workspace_id, plan_activity_id)
            update_log = {'terraform_plan_log': log}
            update_report(test_id, update_log)
            apply_start = datetime.datetime.utcnow().timestamp()
            (apply_activity_id, apply_status) = do_apply(
                test_id, url, workspace_id)
            if apply_activity_id:
                #with open(os.path.join(test_path, 'STATUS'), 'w') as status_file:
                #    status_file.write('APPLY')
                apply_stop = datetime.datetime.utcnow().timestamp()
                update_report(test_id, {
                    'terraform_apply_start': apply_start,
                    'terraform_apply_stop': apply_stop,
                    'terraform_apply_duration': int(apply_stop - apply_start),
                    'terraform_apply_result_code': 0,
                    'terraform_apply_status': apply_status,
                    'terraform_result_code': 0
                })
                log = get_log(url, workspace_id, apply_activity_id)
                update_log = {'terraform_apply_log': log, }
                update_report(test_id, update_log)
            else:
                #with open(os.path.join(test_path, 'STATUS'), 'w') as status_file:
                #    status_file.write('FAILED_APPLY')
                apply_stop = datetime.datetime.utcnow().timestamp()
                update_data = {
                    'terraform_apply_start': apply_start,
                    'terraform_apply_stop': apply_stop,
                    'terraform_apply_duration': int(apply_stop - apply_start),
                    'terraform_apply_result_code': 1,
                    'terraform_result_code': 1,
                    'terraform_apply_status': apply_status
                }
                update_report(test_id, update_data)
                results = {
                    "terraform_failed": "apply failed with status %s" % apply_status
                }
                destroy_start = datetime.datetime.utcnow().timestamp()
                (destroy_activity_id, destroy_status) = do_destroy(
                    test_id, url, workspace_id)
                if destroy_activity_id:
                    #with open(os.path.join(test_path, 'STATUS'), 'w') as status_file:
                    #    status_file.write('DESTROYED')
                    destroy_stop = datetime.datetime.utcnow().timestamp()
                    update_report(test_id, {
                        'terraform_destroy_start': destroy_start,
                        'terraform_destroy_stop': destroy_stop,
                        'terraform_destroy_duration': int(destroy_stop - destroy_start),
                        'terraform_destroy_result_code': 0,
                        'terraform_destroy_status': destroy_status
                    })
                    log = get_log(url, workspace_id, destroy_activity_id)
                    update_log = {'terraform_destroy_log': log, }
                    update_report(test_id, update_log)
                else:
                    #with open(os.path.join(test_path, 'STATUS'), 'w') as status_file:
                    #    status_file.write('FAILED_DESTROY')
                    destroy_stop = datetime.datetime.utcnow().timestamp()
                    update_report(test_id, {
                        'terraform_destroy_start': destroy_start,
                        'terraform_destroy_stop': destroy_stop,
                        'terraform_destroy_duration': int(destroy_stop - destroy_start),
                        'terraform_destroy_result_code': 1,
                        'terraform_destroy_status': destroy_status
                    })
                stop_report(test_id, results)
                delete_workspace(url, workspace_id)
                shutil.rmtree(test_dir)
                return
        else:
            # with open(os.path.join(test_path, 'STATUS'), 'w') as status_file:
            #    status_file.write('FAILED_PLAN')
            plan_stop = datetime.datetime.utcnow().timestamp()
            update_report(test_id,{
                    'terraform_plan_start': plan_start,
                    'terraform_plan_stop': plan_stop,
                    'terraform_plan_duration': int(plan_stop - plan_start),
                    'terraform_plan_result_code': 1,
                    'terraform_plan_status': plan_status
                })
            results = {
                "terraform_failed": "plan failed with status %s" % plan_status
            }
            stop_report(test_id, results)
            delete_workspace(url, workspace_id)
            shutil.rmtree(test_dir)
            return
    else:
        LOG.error('failed to create schematic workspace: %s', create_status)
        LOG.error('POSTed data was %s', json.dumps(data))
        #with open(os.path.join(test_path, 'STATUS'), 'w') as status_file:
        #    status_file.write('FAILED_CREATE')
        start_report(test_id, start_data)
        update_data = {
            'workspace_create_result_code': 1,
            'workspace_create_status': create_status,
            'terraform_result_code': 1,
        }
        update_report(test_id, update_data)
        results = {
            "terraform_failed": "schematics workspace create failed with status %s" % create_status
        }
        stop_report(test_id, results)
        shutil.rmtree(test_dir)
        return

    # workspace complete pool for return
    results = poll_report(test_id)
    if not results:
        results = {"test timedout": "(%d seconds)" %
                   int(CONFIG['test_timeout'])}
        stop_report(test_id, results)
        if 'preserve_timed_out_instances' in CONFIG and CONFIG['preserve_timed_out_instances']:
            #with open(os.path.join(test_path, 'STATUS'), 'w') as status_file:
            #    status_file.write('TIMED_OUT')
            LOG.error(
                'preserving timedout instance for test: %s for debug', test_id)
            os.makedirs(ERRORED_DIR, exist_ok=True)
            shutil.move(test_dir, os.path.join(ERRORED_DIR, test_id))
        else:
            LOG.info('destroying cloud resources for test %s', test_id)
            destroy_start = datetime.datetime.utcnow().timestamp()
            (destroy_activity_id, destroy_status) = do_destroy(
                test_id, url, workspace_id)
            if destroy_activity_id:
                #with open(os.path.join(test_path, 'STATUS'), 'w') as status_file:
                #    status_file.write('DESTROYED')
                destroy_stop = datetime.datetime.utcnow().timestamp()
                update_report(test_id, {
                    'terraform_destroy_start': destroy_start,
                    'terraform_destroy_stop': destroy_stop,
                    'terraform_destroy_duration': int(destroy_stop - destroy_start),
                    'terraform_destroy_result_code': 0,
                    'terraform_destroy_status': destroy_status
                })
                log = get_log(url, workspace_id, destroy_activity_id)
                update_log = {'terraform_apply_log': log, }
                update_report(test_id, update_log)
            else:
                #with open(os.path.join(test_path, 'STATUS'), 'w') as status_file:
                #    status_file.write('FAILED_DESTROY')
                destroy_stop = datetime.datetime.utcnow().timestamp()
                update_report(test_id, {
                    'terraform_destroy_start': destroy_start,
                    'terraform_destroy_stop': destroy_stop,
                    'terraform_destroy_duration': int(destroy_stop - destroy_start),
                    'terraform_destroy_result_code': 1,
                    'terraform_result_code': 1,
                    'terraform_destroy_status': destroy_status
                })
            delete_workspace(url, workspace_id)
            shutil.rmtree(test_dir)
    else:
        if results['results']['status'] == "ERROR":
            #with open(os.path.join(test_path, 'STATUS'), 'w') as status_file:
            #    status_file.write('ERRORED')
            if 'preserve_errored_instances' in CONFIG and CONFIG['preserve_errored_instances']:
                LOG.error(
                    'preserving errored instance for test: %s for debug', test_id)
                os.makedirs(ERRORED_DIR, exist_ok=True)
                shutil.move(test_dir, os.path.join(ERRORED_DIR, test_id))
            else:
                LOG.error(
                    'destroying cloud resources for errored test %s', test_id)
                destroy_start = datetime.datetime.utcnow().timestamp()
                (destroy_activity_id, destroy_status) = do_destroy(
                    test_id, url, workspace_id)
                if destroy_activity_id:
                    #with open(os.path.join(test_path, 'STATUS'), 'w') as status_file:
                    #    status_file.write('DESTROYED')
                    destroy_stop = datetime.datetime.utcnow().timestamp()
                    update_report(test_id, {
                        'terraform_destroy_start': destroy_start,
                        'terraform_destroy_stop': destroy_stop,
                        'terraform_destroy_duration': int(destroy_stop - destroy_start),
                        'terraform_destroy_result_code': 0,
                        'terraform_destroy_status': destroy_status
                    })
                    log = get_log(url, workspace_id, destroy_activity_id)
                    update_log = {'terraform_apply_log': log, }
                    update_report(test_id, update_log)
                else:
                    #with open(os.path.join(test_path, 'STATUS'), 'w') as status_file:
                    #    status_file.write('FAILED_DESTROY')
                    destroy_stop = datetime.datetime.utcnow().timestamp()
                    update_report(test_id, {
                        'terraform_destroy_start': destroy_start,
                        'terraform_destroy_stop': destroy_stop,
                        'terraform_destroy_duration': int(destroy_stop - destroy_start),
                        'terraform_destroy_result_code': 1,
                        'terraform_result_code': 1,
                        'terraform_destroy_status': destroy_status
                    })
                delete_workspace(url, workspace_id)
                shutil.rmtree(test_dir)
        else:
            LOG.info('destroying cloud resources for completed test %s', test_id)
            destroy_start = datetime.datetime.utcnow().timestamp()
            (destroy_activity_id, destroy_status) = do_destroy(
                test_id, url, workspace_id)
            if destroy_activity_id:
                #with open(os.path.join(test_path, 'STATUS'), 'w') as status_file:
                #    status_file.write('DESTROYED')
                destroy_stop = datetime.datetime.utcnow().timestamp()
                update_report(test_id, {
                    'terraform_destroy_start': destroy_start,
                    'terraform_destroy_stop': destroy_stop,
                    'terraform_destroy_duration': int(destroy_stop - destroy_start),
                    'terraform_destroy_result_code': 0,
                    'terraform_destroy_status': destroy_status
                })
                log = get_log(url, workspace_id, destroy_activity_id)
                update_log = {'terraform_apply_log': log, }
                update_report(test_id, update_log)
            else:
                #with open(os.path.join(test_path, 'STATUS'), 'w') as status_file:
                #    status_file.write('FAILED_DESTROY')
                destroy_stop = datetime.datetime.utcnow().timestamp()
                update_report(test_id, {
                    'terraform_destroy_start': destroy_start,
                    'terraform_destroy_stop': destroy_stop,
                    'terraform_destroy_duration': int(destroy_stop - destroy_start),
                    'terraform_destroy_result_code': 1,
                    'terraform_result_code': 1,
                    'terraform_destroy_status': destroy_status
                })
            delete_workspace(url, workspace_id)
            if 'keep_completed_state' in CONFIG and CONFIG['keep_completed_state']:
                shutil.move(test_dir, os.path.join(COMPLETE_DIR, test_id))
            else:
                shutil.rmtree(test_dir)


def initialize_test_dir(test_path):
    test_dir = os.path.basename(test_path)
    test_path_parts = test_path.split(os.path.sep)
    zone = test_path_parts[(len(test_path_parts) - 4)]
    image = test_path_parts[(len(test_path_parts) - 3)]
    ttype = test_path_parts[(len(test_path_parts) - 2)]
    dest = os.path.join(RUNNING_DIR, test_dir)
    shutil.move(test_path, dest)
    return (zone, image, ttype, dest)


def build_pool():
    pool = []
    zones_dir = os.listdir(QUEUE_DIR)
    for zone in zones_dir:
        images_dir = os.path.join(QUEUE_DIR, zone)
        zone_images = os.listdir(images_dir)
        for zi in zone_images:
            tts_dir = os.path.join(images_dir, zi)
            tts = os.listdir(tts_dir)
            for tt in tts:
                tests_dir = os.path.join(tts_dir, tt)
                tests = os.listdir(tests_dir)
                for test in tests:
                    pool.append(os.path.join(tests_dir, test))
    return pool


def requeue_running():
    for rt in os.listdir(RUNNING_DIR):
        test_path = os.path.join(RUNNING_DIR, rt)
        varfile_path = os.path.join(test_path, 'test_vars.json')
        if os.path.exists(varfile_path):
            varfile = open(varfile_path, 'r')
            rvars = json.load(varfile)
            zone = rvars['zone']
            template_type = rvars['template_type']
            image = rvars['tmos_image_name']
            queue_path = os.path.join(QUEUE_DIR, zone, image, template_type)
            os.makedirs(queue_path, exist_ok=True)
            shutil.move(test_path, os.path.join(queue_path, rt))
        else:
            LOG.error('invalid test %s found ... removing', test_path)
            shutil.rmtree(test_path)


def runner():
    requeue_running()
    test_pool = build_pool()
    random.shuffle(test_pool)
    with concurrent.futures.ThreadPoolExecutor(max_workers=CONFIG['thread_pool_size']) as executor:
        for test_path in test_pool:
            executor.submit(run_test, test_path=test_path)


def initialize():
    global MY_PID, CONFIG, WORKSPACE_CREATE_TIMEOUT, \
        WORKSPACE_PLAN_TIMEOUT, WORKSPACE_APPLY_TIMEOUT, \
        WORKSPACE_DESTROY_TIMEOUT, WORKSPACE_DELETE_TIMEOUT, \
        WORKSPACE_STATUS_POLL_INTERVAL, WORKSPACE_RETRY_INTERVAL, \
        REPORT_REQUEST_INTERVAL
    MY_PID = os.getpid()
    os.makedirs(QUEUE_DIR, exist_ok=True)
    os.makedirs(RUNNING_DIR, exist_ok=True)
    os.makedirs(COMPLETE_DIR, exist_ok=True)
    config_json = ''
    with open(CONFIG_FILE, 'r') as cf:
        config_json = cf.read()
    config = json.loads(config_json)
    # intialize missing config defaults
    CONFIG = config
    if 'workspace_create_timeout' in config:
        WORKSPACE_CREATE_TIMEOUT = config['workspace_create_timeout']
    if 'workspace_plan_timeout' in config:
        WORKSPACE_PLAN_TIMEOUT = config['workspace_plan_timeout']
    if 'workspace_apply_timeout' in config:
        WORKSPACE_APPLY_TIMEOUT = config['workspace_apply_timeout']
    if 'workspace_destroy_timeout' in config:
        WORKSPACE_DESTROY_TIMEOUT = config['workspace_destroy_timeout']
    if 'workspace_delete_timeout' in config:
        WORKSPACE_DELETE_TIMEOUT = config['workspace_delete_timeout']
    if 'workspace_status_poll_interval' in config:
        WORKSPACE_STATUS_POLL_INTERVAL = config['workspace_status_poll_interval']
    if 'workspace_retry_interval' in config:
        WORKSPACE_RETRY_INTERVAL = config['workspace_retry_interval']
    if 'report_request_interval' in config:
        REPORT_REQUEST_INTERVAL = config['report_request_interval']


if __name__ == "__main__":
    START_TIME = time.time()
    LOG.debug('process start time: %s', datetime.datetime.fromtimestamp(
        START_TIME).strftime("%A, %B %d, %Y %I:%M:%S"))
    initialize()
    runner()
    ERROR_MESSAGE = ''
    ERROR = False

    STOP_TIME = time.time()
    DURATION = STOP_TIME - START_TIME
    LOG.debug(
        'process end time: %s - ran %s (seconds)',
        datetime.datetime.fromtimestamp(
            STOP_TIME).strftime("%A, %B %d, %Y %I:%M:%S"),
        DURATION
    )
