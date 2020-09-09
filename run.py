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
import base64

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
SESSION_SECONDS = 1800
REQUEST_RETRIES = 10
REQUEST_DELAY = 10

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
        time.sleep(CONFIG['report_request_frequency'])
    return None


def get_iam_token():
    global SESSION_TOKEN, REFRESH_TOKEN, SESSION_TIMESTAMP
    now = int(time.time())
    if SESSION_TIMESTAMP > 0 and ((now - SESSION_TIMESTAMP) < SESSION_SECONDS):
        return SESSION_TOKEN
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/x-www-form-urlencoded"
    }
    data = "apikey=%s&grant_type=urn:ibm:params:oauth:grant-type:apikey" % CONFIG['api_key']
    response = requests.post(AUTH_ENDPOINT, headers=headers, data=data)
    if response.status_code < 300:
        SESSION_TIMESTAMP = int(time.time())
        response_json = response.json()
        SESSION_TOKEN = response_json['access_token']
        REFRESH_TOKEN = response_json['refresh_token']
        return SESSION_TOKEN
    else:
        LOG.error('could not get an access token %d - %s',
                  response.status_code, response.content)
        return None


def get_refresh_token():
    now = int(time.time())
    if SESSION_TIMESTAMP > 0 and ((now - SESSION_TIMESTAMP) < SESSION_SECONDS):
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
        time.sleep(1)
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
    if response.status_code < 300:
        workspace_id = response.json()['id']
        status_url = "%s/%s" % (url, workspace_id)
        LOG.info('polling for workspace create to complete for %s', test_id)
        status_returned = poll_workspace_until(
            status_url, ['inactive', 'failed', 'template error'], 300)
        if status_returned:
            if status_returned.lower() == 'inactive':
                return (workspace_id, status_returned)
            else:
                LOG.error('workspace %s returned status %s - failed to create',
                          workspace_id, status_returned)
                return (None, "created workspace %s returned status %s" % (workspace_id, status_returned))
        else:
            return (None, "create reqeust to %s timed-out" % url)
    else:
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
    creds = 'apikey:%s' % CONFIG['api_key']
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Authorization": "Basic %s" % base64.b64encode(creds.encode('ascii'))
    }
    response = requests.post(plan_url, headers=headers)
    LOG.info('workspace plan returned %d for %s',
             response.status_code, test_id)
    while response.status_code == 409:
        LOG.debug('workspace locked.. retrying')
        time.sleep(2)
        response = requests.post(plan_url, headers=headers)
        LOG.info('workspace plan returned %d for %s',
                 response.status_code, test_id)
    if response.status_code < 300:
        activity_id = response.json()['activityid']
        status_url = "%s/%s" % (url, workspace_id)
        LOG.info('polling for workspace plan to initiate for %s', test_id)
        status_returned = poll_workspace_until(
            status_url, ['inprogress', 'failed'], 300
        )
        if status_returned:
            if status_returned.lower() == 'failed':
                status_message = "workspace %s plan with activity_id %s returned status %s" % (
                    workspace_id, activity_id, status_returned)
                return (None, status_message)
            else:
                LOG.info('polling for workspace plan to complete for %s', test_id)
                status_returned = poll_workspace_until(
                    status_url, ['inactive', 'failed'], 300)
                if status_returned:
                    if status_returned.lower() == 'inactive':
                        return (activity_id, status_returned)
                    else:
                        status_message = "workspace %s plan with activity_id %s returned status %s" % (
                            workspace_id, activity_id, status_returned)
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
        time.sleep(2)
        response = requests.post(apply_url, headers=headers)
        LOG.info('workspace apply returned %d for %s',
                 response.status_code, test_id)

    if response.status_code < 300:
        activity_id = response.json()['activityid']
        status_url = "%s/%s" % (url, workspace_id)
        LOG.info('polling for workspace apply to complete for %s', test_id)
        status_returned = poll_workspace_until(
            status_url, ['active', 'failed'], 300)
        if status_returned:
            if status_returned.lower() == 'active':
                return (activity_id, status_returned)
            else:
                status_message = "workspace %s apply with activity_id %s returned status %s" % (
                    workspace_id, activity_id, status_returned)
                return (None, status_message)
        else:
            status_message = "workspace %s apply timed-out" % apply_url
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
        status_url, ['inactive', 'active', 'failed'], 300)
    delete_url = "%s/%s" % (url, workspace_id)
    if status_returned.lower() == 'active':
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
        time.sleep(2)
        response = requests.delete(delete_url, headers=headers)
        LOG.info('workspace plan returned %d for %s',
                 response.status_code, workspace_id)
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
    if response.status_code < 300:
        return response.json()
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
    (workspace_id, create_status) = create_workspace(test_id, url, data)
    if workspace_id:
        start_report(test_id, start_data)
        update_report(
            test_id, {"workspace_id": workspace_id, "create_status": create_status})
        (plan_activity_id, plan_status) = do_plan(test_id, url, workspace_id)
        if plan_activity_id:
            update_report(
                test_id, {"workspace_id": workspace_id, "plan_status": create_status})
            log = get_log(url, workspace_id, plan_activity_id)
            update_log = {"workspace_plan_log": log}
            update_report(test_id, update_log)
            (apply_activity_id, apply_status) = do_apply(
                test_id, url, workspace_id)
            if apply_activity_id:
                update_report(
                    test_id, {"workspace_id": workspace_id, "apply_status": apply_status})
                log = get_log(url, workspace_id, apply_activity_id)
                now = datetime.datetime.utcnow()
                update_data = {
                    'terraform_apply_result_code': 0,
                    'worspace_apply_log': log,
                    'terraform_apply_completed_at': now.timestamp(),
                    'terraform_apply_completed_at_readable': now.strftime('%Y-%m-%d %H:%M:%S UTC')
                }
                update_report(test_id, update_data)
            else:
                update_data = {
                    'terraform_apply_result_code': 1,
                }
                update_report(test_id, update_data)
                results = {
                    "terraform_failed": "workspace apply failed with status %s" % apply_status
                }
                stop_report(test_id, results)
                if 'preserve_errored_instances' in CONFIG and CONFIG['preserve_errored_instances']:
                    LOG.error(
                        'preserving errored instance for test: %s for debug', test_id)
                    os.makedirs(ERRORED_DIR, exist_ok=True)
                    shutil.move(test_dir, os.path.join(ERRORED_DIR, test_id))
                else:
                    delete_workspace(url, workspace_id)
                    shutil.rmtree(test_dir)
                return
        else:
            update_data = {
                'terraform_apply_result_code': 1,
            }
            update_report(test_id, update_data)
            results = {
                "terraform_failed": "workspace plan failed with status %s" % plan_status
            }
            stop_report(test_id, results)
            if 'preserve_errored_instances' in CONFIG and CONFIG['preserve_errored_instances']:
                LOG.error(
                    'preserving errored instance for test: %s for debug', test_id)
                os.makedirs(ERRORED_DIR, exist_ok=True)
                shutil.move(test_dir, os.path.join(ERRORED_DIR, test_id))
            else:
                delete_workspace(url, workspace_id)
                shutil.rmtree(test_dir)
            return
    else:
        LOG.error('failed to create workspace: %s', create_status)
        LOG.error('POSTed data was %s', json.dumps(data))
        start_report(test_id, start_data)
        update_data = {
            'terraform_apply_result_code': 1,
        }
        update_report(test_id, update_data)
        results = {
            "terraform_failed": "workspace create failed with status %s" % create_status
        }
        stop_report(test_id, results)
        if 'preserve_errored_instances' in CONFIG and CONFIG['preserve_errored_instances']:
            LOG.error(
                'preserving errored instance for test: %s for debug', test_id)
            os.makedirs(ERRORED_DIR, exist_ok=True)
            shutil.move(test_dir, os.path.join(ERRORED_DIR, test_id))
        else:
            shutil.rmtree(test_dir)
        return

    # workspace complete pool for return
    results = poll_report(test_id)
    if not results:
        results = {"test timedout": "(%d seconds)" %
                   int(CONFIG['test_timeout'])}
        stop_report(test_id, results)
        if 'preserve_timed_out_instances' in CONFIG and CONFIG['preserve_timed_out_instances']:
            LOG.error(
                'preserving timedout instance for test: %s for debug', test_id)
            os.makedirs(ERRORED_DIR, exist_ok=True)
            shutil.move(test_dir, os.path.join(ERRORED_DIR, test_id))
        else:
            LOG.info('destroying cloud resources for test %s', test_id)
            delete_workspace(url, workspace_id)
            shutil.rmtree(test_dir)
    else:
        if results['results']['status'] == "ERROR":
            if 'preserve_errored_instances' in CONFIG and CONFIG['preserve_errored_instances']:
                LOG.error(
                    'preserving errored instance for test: %s for debug', test_id)
                os.makedirs(ERRORED_DIR, exist_ok=True)
                shutil.move(test_dir, os.path.join(ERRORED_DIR, test_id))
            else:
                LOG.error(
                    'destroying cloud resources for errored test %s', test_id)
                delete_workspace(url, workspace_id)
                shutil.rmtree(test_dir)
        else:
            LOG.info('destroying cloud resources for completed test %s', test_id)
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
    global MY_PID, CONFIG
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
