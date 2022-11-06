import logging
import time
import urllib.request as urlrequest
import urllib.error as urlerror
import urllib.parse as urlparse
import json, csv
import os, base64
from datetime import datetime, timezone
import ciso8601
from coralogix.handlers import CoralogixLogger
import boto3
from boto3.dynamodb.conditions import Key

# Create internal logger and logs logger.
internal_logger = logging.getLogger('Internal Logger')
internal_logger.setLevel(logging.INFO)
external_logger = logging.getLogger('External Logger')
external_logger.setLevel(logging.INFO)
external_logger.propagate = False
# Define environment variables
PRIVATE_KEY = os.getenv('CORALOGIX_PRIVATE_KEY')
APP_NAME = os.getenv('CORALOGIX_APPLICATION_NAME')
SUB_SYSTEM = os.getenv('CORALOGIX_SUBSYSTEM_NAME')
SANDBOX_ENV = os.getenv('SF_SANDBOX_ENV', 'False')
HOST = os.getenv('SF_HOST')
LOGS_TO_STDOUT = os.getenv('LOGS_TO_STDOUT', 'True')
EVENT_TYPE = os.getenv('SF_EVENT_TYPE', '')
CLIENT_ID = os.getenv('SF_CLIENT_ID')
CLIENT_SECRET = os.getenv('SF_CLIENT_SECRET')
USERNAME = os.getenv('SF_USERNAME')
PASSWORD = os.getenv('SF_PASSWORD')
DYNAMODB_TABLE = os.getenv('DYNAMODB_TABLE')
TRUE_VALUES = ['True','true']
ALLOWED_EVENT_TYPE = ['API', 'ApexCallout', 'ApexExecution', 'AsyncReportRun', 'ApexRestApi', 'ApexTrigger', 'ApiTotalUsage', 'AuraRequest', 'ApexUnexpectedException', 'BulkApi', 'BulkApi2', 'ContentDistribution', 'ContentDocumentLink', 'ChangeSetOperation', 'ContentTransfer',
 'CorsViolation', 'Dashboard', 'DocumentAttachmentDownloads', 'ExternalCustomApexCallout', 'ExternalODataCallout', 'FlowExecution', 'KnowledgeArticleView', 'Login', 'LoginAs', 'Logout', 'LightningError', 'LightningInteraction', 'LightningPerformance', 'LightningPageView',
  'MetadataApiOperation', 'NamedCredential', 'OneCommerceUsage', 'PackageInstall', 'QueuedExecution', 'PlatformEncryption', 'Report', 'RestApi', 'Sites', 'SearchClick', 'Search', 'TimeBasedWorkflow', 'URI', 'VisualforceRequest', 'WaveChange', 'WaveInteraction', 'WavePerformance']
def lambda_handler(event, context):
    internal_logger.info('Event-log puller lambda - init')
    # Coralogix variables check
    if PRIVATE_KEY == '':
        internal_logger.error('Event-log puller lambda Failure - coralogix private key not found')
        return {
            'statusCode': 400,
            'body': json.dumps({
            'message': 'Event-log puller lambda Failure - coralogix private key not found',
            }),
        }
    if APP_NAME == '' or SUB_SYSTEM == '':
        internal_logger.error('Event-log puller lambda Failure - coralogix application name and subsystem name not found')
        return {
            'statusCode': 400,
            'body': json.dumps({
            'message': 'Event-log puller lambda Failure - coralogix application name and subsystem name not found',
            }),
        }
    # print to stdout/cloudwatch external logger's logs
    if LOGS_TO_STDOUT in TRUE_VALUES:
        external_logger.propagate = True
    # Coralogix Logger init
    coralogix_external_handler = CoralogixLogger(PRIVATE_KEY, APP_NAME, SUB_SYSTEM)
    # Add coralogix logger as a handler to the standard Python logger.
    external_logger.addHandler(coralogix_external_handler)
    # Environment variables check
    if HOST == '':
        internal_logger.error('Event-log puller lambda Failure - salesforce host not found')
        return {
            'statusCode': 400,
            'body': json.dumps({
            'message': 'Event-log puller lambda Failure - salesforce host not found',
            }),
        }
    if CLIENT_ID == '' or CLIENT_SECRET == '':
        internal_logger.error('Event-log puller lambda Failure - Event-log client_id and client_secret not found')
        return {
            'statusCode': 400,
            'body': json.dumps({
            'message': 'Event-log puller lambda Failure - Event-log client_id and client_secret not found',
            }),
        }
    if USERNAME == '' or PASSWORD == '':
        internal_logger.error('Event-log puller lambda Failure - Event-log username and password not found')
        return {
            'statusCode': 400,
            'body': json.dumps({
            'message': 'Event-log puller lambda Failure - Event-log username and password not found',
            }),
        }
    if EVENT_TYPE != '' and EVENT_TYPE not in ALLOWED_EVENT_TYPE :
        internal_logger.error('Event-log puller lambda Failure - event type not found')
        return {
            'statusCode': 400,
            'body': json.dumps({
            'message': 'Event-log puller lambda Failure - event tpye not found',
            }),
        } 
    if DYNAMODB_TABLE is None or DYNAMODB_TABLE == '':
        internal_logger.error('Event-log puller lambda Failure - dynamoDB not found')
        return {
            'statusCode': 400,
            'body': json.dumps({
            'message': 'Event-log puller lambda Failure - dynamoDB not found',
            }),
        }   
    internal_logger.info('Event-log puller lambda - init complete')
    access_token = get_token()
    # check if failed to get token
    if isinstance(access_token, dict):
        return access_token
    # dynamodb init
    dynamodb = boto3.resource('dynamodb')
    db_table = dynamodb.Table(DYNAMODB_TABLE)
    # get all records
    db_response = db_table.scan()
    # get last_update value
    if 'Items' in db_response and len(db_response['Items']) > 0:
        last_update = [x for x in db_response['Items'] if x.id == 0][0].lastUpdate
    else:
        last_update = datetime.today(timezone.utc).isoformat().replace('+00:00', 'z')
    # build SF domain
    if SANDBOX_ENV in TRUE_VALUES:
        domain = 'https://%s.my.salesforce.com' % HOST
    else:
        domain = 'https://%s.sandbox.my.salesforce.com' % HOST
    records = get_records_list(access_token, domain, last_update)
    if records == None:
        return {
        'statusCode': 200,
        'body': json.dumps({
        'message': 'Event-log puller lambda Failure could not retrieve records - Endpoint: %s' % HOST,
        }),
        }
    # setup lambda timout flag
    early_end = False
    if len(records) == 0:
        # no records from SF, modify last update to today.
        db_table.put_item(Item={
        "id": 0,
        "lastUpdated": datetime.today(timezone.utc).isoformat().replace('+00:00', 'z')
        })
        # check if any records needs to be deleted
        new_last_update = ciso8601.parse_datetime(datetime.today(timezone.utc).isoformat())
        for record in db_response['Items']:
            if ciso8601.parse_datetime(record.lastUpdated) < new_last_update:
                db_table.delete_item(Key={
                    "id": record.id
                })
    else:
        # setup early_end record index
        early_last_record = len(records) - 1
        # iterate sf_records list
        for i, record in enumerate(records):
            db_record = [x for x in db_response['Items'] if x.id == record.Id]
            if len(db_record) == 0 :
                # record not found in db, save it and send logs to coralogix
                # check for errors
                if record_logic() is not None:
                    db_table.put_item(Item={
                        "id": record.Id,
                        "lastUpdated": record.LogDate.replace('+0000','z')
                    })
            if context.get_remaining_time_in_millis() < 30000:
                early_last_record = i
                early_end = True
                break
        # save to db the latest date
        last_record = records[early_last_record]
        db_table.put_item(Item={
            "id": 0,
            "lastUpdated": last_record.LogDate.replace('+0000','z')
        })
        new_last_update = ciso8601.parse_datetime(last_record.LogDate)
        # loop on db_records to remove old ones
        for record in db_response['Items']:
            if ciso8601.parse_datetime(record.lastUpdated) < new_last_update:
                db_table.delete_item(Key={
                    "id": record.id
                })

    CoralogixLogger.flush_messages()
    time.sleep(5) # for now until fixing python sdk not fully flushing within aws lambda
    if early_end: # if the lambda had to stop before finishing due to time-out
        internal_logger.warn("Event-log puller lambda - Not enough time to send all logs, waiting for next invocation")
        return {
        "statusCode": 200,
        "body": json.dumps({
        "message": "Event-log puller lambda - Not enough time to send all logs, waiting for next invocation",
        }),
        }
    else:
        internal_logger.info('Event-log puller lambda Success - logs sent to coralogix')
        return {
        'statusCode': 200,
        'body': json.dumps({
        'message': 'Event-log puller lambda Success - logs sent to coralogix',
        }),
        }

def get_records_list(access_token, domain, last_update):
    endpoint = domain + "/services/data/v55.0/query?q=SELECT+Id+,+EventType+,+LogFile+,+LogDate+,+LogFileLength+,Interval+FROM+EventLogFile+WHERE+LogDate+>+%s" % last_update
    if EVENT_TYPE != '':
        endpoint += "+AND+EventType+=+'%s'" % EVENT_TYPE
    endpoint += "+ORDER BY+LogDate+ASC"
    request = urlrequest.Request(endpoint,method='get')
    request.add_header('Authorization','Bearer %s' % access_token)
    request.add_header('User-Agent', 'Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:33.0) Gecko/20100101 Firefox/33.0')
    request.add_header('host', domain)
    try:
        response = urlrequest.urlopen(request, timeout=30)
    except urlerror.HTTPError as e:
        internal_logger.error('Event-log puller lambda Failure could not retrieve records - Endpoint: %s , error: %s' % (HOST, e))
        return None
    except urlerror.URLError as e:
        internal_logger.error('Event-log puller lambda Failure could not retrieve records - Endpoint: %s , error: %s' % (HOST, e))
        return None
    res_body = response.read()
    try:
        JSON_object = json.loads(res_body.decode('utf-8'))
        records = JSON_object['records']
        return records
    except (ValueError,KeyError):
        internal_logger.error('Event-log puller lambda Failure - could not retrieve records, failed to get records from response - %s ' % json.dumps(JSON_object))
        return None

def record_logic(access_token, domain, record):
    endpoint = domain + "/services/data/v55.0/sobjects/EventLogFile/%s/LogFile" % record.LogFile
    request = urlrequest.Request(endpoint,method='get')
    request.add_header('Authorization','Bearer %s' % access_token)
    request.add_header('User-Agent', 'Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:33.0) Gecko/20100101 Firefox/33.0')
    request.add_header('host', domain)
    try:
        response = urlrequest.urlopen(request, timeout=30)
    except urlerror.HTTPError as e:
        internal_logger.error('Event-log puller lambda Failure could not retrieve logfile - recordId: %s,  Endpoint: %s , error: %s' % (record.Id, domain, e))
        return None
    except urlerror.URLError as e:
        internal_logger.error('Event-log puller lambda Failure could not retrieve logfile - recordId: %s,  Endpoint: %s , error: %s' % (record.Id, domain, e))
        return None
    res_body = response.read()
    try:
        data = {}
        csvReader = csv.DictReader(res_body.decode('utf-8'))
        for i,row in enumerate(csvReader):
            data[i] = row
        log_sender(data)
        return 1
    except:
        internal_logger.error('Event-log puller lambda Failure could not convert csv file to json - recordId: %s,  Endpoint: %s , error: %s' % (record.Id, domain, e))
        return None

def log_sender(data):
    for row in data:
        external_logger.info(json.dumps(row))

def get_token():
    form_data = {
        'username': USERNAME,
        'password': PASSWORD,
        'grant_type': 'password',
        'client_id': CLIENT_ID,
        'client_secret': CLIENT_SECRET
        }
    req_data = urlparse.urlencode(form_data)
    req_data = req_data.encode('utf-8')
    prefix = 'login'
    if SANDBOX_ENV in TRUE_VALUES:
        prefix = 'test'
    auth_host = "https://%s.salesforce.com/services/oauth2/token" % prefix
    request = urlrequest.Request(auth_host,req_data)
    # adding charset parameter to the Content-Type header.
    request.add_header('Content-Type', 'application/x-www-form-urlencoded;charset=utf-8')
    request.add_header('User-Agent', 'Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:33.0) Gecko/20100101 Firefox/33.0')
    try:
        response = urlrequest.urlopen(request, timeout=15)
    except urlerror.HTTPError as e:
        internal_logger.error('Event-log puller lambda Failure - Error while getting token, Error: %s' % e)
        return {
           'statusCode': e.code,
            'body': json.dumps({
            'message': 'Event-log puller lambda Failure - Error while getting token, Error: %s' % e,
        }),
        }
    except urlerror.URLError as e:
        internal_logger.error('Event-log puller lambda Failure - Error while getting token, Error: %s' % e)
        return {
           'statusCode': 400,
            'body': json.dumps({
            'message': 'Event-log puller lambda Failure - Error while getting token, Error: %s' % e,
        }),
        }
    res_body = response.read()
    try:
        JSON_object = json.loads(res_body.decode('utf-8'))
        access_token = JSON_object['access_token']
        return access_token
    except (ValueError,KeyError):
        internal_logger.error('Event-log puller lambda Failure - Error while getting token, failed to get access_token from response - %s ' % json.dumps(JSON_object))
        return {
           'statusCode': 400,
            'body': json.dumps({
            'message': 'Event-log puller lambda Failure - Error while getting token, failed to get access_token from request - %s ' % json.dumps(JSON_object),
        }),
        }
