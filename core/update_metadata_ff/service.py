# -*- coding: utf-8 -*-
from core import sbg_utils, utils, ff_utils
import boto3

s3 = boto3.resource('s3')


# check the status and other details of import
# TODO: this is for Workflow, should be generalizable
# pass in object and schema name, does update / insert
def handler(event, context):
    '''
    this is to check if the task run is done:
    http://docs.sevenbridges.com/reference#get-task-execution-details
    consider this format:
      {'data': event.get('workflow'), # actully in reverse
       'data_name': 'workflow', # how to get the data
       'conversion_routine': 'function to run, taking workflow as argument'
       }
       then put / patch data_name with data
    #
    '''
    # used to automatically determine the environment
    tibanna_settings = event.get('_tibanna', {})
    tibanna = utils.Tibanna(**tibanna_settings)
    sbg = sbg_utils.create_sbg_workflow(token=tibanna.sbg_keys, **event.get('workflow'))
    # run_response = event.get('run_response')
    ff_meta = ff_utils.create_ffmeta(sbg, **event.get('ff_meta'))
    key = event.get('ff_keys')
    ff_keys = utils.get_access_keys() if not key else key

    workflow_post_resp = ff_meta.post(key=ff_keys)

    return{"workflow": sbg.as_dict(),
           "res": workflow_post_resp,
           "ff_meta": ff_meta.as_dict(),
           "_tibanna": tibanna.as_dict()}
