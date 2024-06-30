import boto3
import os
import json
import logging

from botocore.exceptions import ClientError

# use powertools to accept event-bridge event
from aws_lambda_powertools.utilities.data_classes import event_source, EventBridgeEvent
from aws_lambda_powertools.utilities.typing import LambdaContext
from aws_lambda_powertools import Logger
from tenacity import Retrying
from tenacity.before_sleep import before_sleep_log
from tenacity.stop import stop_after_attempt
from tenacity.wait import wait_exponential_jitter
from tenacity.retry import retry_if_exception_type
from tenacity import Retrying

logger = Logger()

ib_account_id = os.environ.get("IB_ACCOUNT_ID")
ib_account_region = os.environ.get("IB_REGION")


class ThrottlingException(Exception):
    """To represent 5XX errors from the request"""

    pass


retry_automation = Retrying(
    reraise=True,
    retry=retry_if_exception_type(ThrottlingException),
    wait=wait_exponential_jitter(initial=5, exp_base=1.43, max=280),
    stop=stop_after_attempt(10),
    before_sleep=before_sleep_log(logger, logging.WARNING),
)


def response_message(status_code, message):
    message = {
        "statusCode": status_code,
        "body": json.dumps(message),
    }
    return message


@retry_automation.wraps
def assume_role(
    account_id, region_name, role_name="AWS_PLATFORM_AutomationExecutionRole"
):
    try:
        sts_client = boto3.client("sts", region_name=region_name)
        response = sts_client.assume_role(
            RoleArn=f"arn:aws:iam::{account_id}:role/{role_name}",
            RoleSessionName="LaunchSSMAutomationSession",
        )
        credentials = response["Credentials"]
        return boto3.client(
            "ssm",
            region_name=region_name,
            aws_access_key_id=credentials["AccessKeyId"],
            aws_secret_access_key=credentials["SecretAccessKey"],
            aws_session_token=credentials["SessionToken"],
        )
    except ClientError as err:
        error_code = err.response["Error"]["Code"]
        if error_code == "ThrottlingException":
            logger.error(f"ThrottlingException occurred: {err}", exc_info=True)
            raise ThrottlingException(err)
        else:
            logger.error(f"An error occurred: {err}", exc_info=True)
            raise err


@retry_automation.wraps
def start_automation_execution(
    _ib_account_id, _spoke_region, _instance_id, ssm_client=None
):
    document_arn = f"arn:aws:ssm:{_spoke_region}:{_ib_account_id}:document/DomainJoinAutomation"

    try:
        response = ssm_client.start_automation_execution(
            DocumentName=document_arn,
            Parameters={
                "InstanceId": [_instance_id],
            },
        )
        return response

    except ClientError as err:
        error_code = err.response["Error"]["Code"]
        if error_code == "ThrottlingException":
            logger.error(f"ThrottlingException occurred: {err}", exc_info=True)
            raise ThrottlingException(err)
        else:
            logger.error(f"An error occurred: {err}", exc_info=True)
            raise err


@event_source(data_class=EventBridgeEvent)
@logger.inject_lambda_context(log_event=True)
def lambda_handler(event: EventBridgeEvent, context: LambdaContext) -> dict:
    spoke_account_id = event.account
    spoke_region = event.region
    instance_id = event.detail["instance-id"]

    message = {
        "instance_id": instance_id,
        "spoke_region": spoke_region,
        "spoke_account_id": spoke_account_id,
    }

    try:
        ssm_client = assume_role(spoke_account_id, spoke_region)
        response = start_automation_execution(
            ib_account_id,
            spoke_region,
            instance_id,
            ssm_client,
        )
        message[
            "description"
        ] = f"SSM Automation succeeded to execute within execution id {response['AutomationExecutionId']}"

    except ClientError as er:
        logger.error(er, exc_info=True)
        message["description"] = "Failed to start automation execution"
        logger.critical(message)
        raise er

    except Exception as err:
        logger.error(err, exc_info=True)
        message["description"] = "Unknown error occurred"
        logger.critical(message)
        raise err

    logger.info(message)
    return response_message(response["ResponseMetadata"]["HTTPStatusCode"], message)
