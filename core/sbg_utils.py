from __future__ import print_function
import json
import logging
import datetime
import requests
import random
from core.utils import ensure_list

###########################
# Config
###########################
SBG_PROJECT_ID = "4dn-dcic/dev"
LOG = logging.getLogger(__name__)


class SBGStillRunningException(Exception):
    pass


def to_sbg_workflow_args(parameter_dict, vals_as_string=False):
    metadata_parameters = []
    metadata_input = []
    for k, v in parameter_dict.iteritems():
        # we need this to be a float or integer if it really is, else a string
        if not vals_as_string:
            try:
                v = float(v)
                if v % 1 == 0:
                    v = int(v)
            except ValueError:
                v = str(v)
        else:
            v = str(v)

        metadata_parameters.append({"workflow_argument_name": k, "value": v})

    return (metadata_parameters, metadata_input)


def create_sbg_volume_details(volume_suffix=None, volume_id=None, volume_prefix='4dn_s3'):
    prefix = volume_prefix
    account = '4dn-labor'
    id = ''
    name = ''
    if volume_id is not None:
        id = volume_id
        name = id.split('/')[1]

    else:
        if volume_suffix is None:
            volume_suffix = ''
            for i in xrange(8):
                r = random.choice('abcdefghijklmnopqrstuvwxyz1234567890')
                volume_suffix += r

        name = prefix + volume_suffix  # name of the volume to be mounted on sbg.
        id = account + '/' + name    # ID of the volume to be mounted on sbg.

    return {'id': id.lower(), 'name': name.lower()}


def create_sbg_workflow(app_name, token, task_id='', task_input=None,
                        project_id=None, import_id_list=None, mounted_volume_ids=None,
                        export_id_list=None, **kwargs):
    '''
    helper function to be used to create object from serialized json dictionary
    '''
    if not project_id:
        project_id = SBG_PROJECT_ID

    if not mounted_volume_ids and kwargs.get('volume_list'):
        mounted_volume_ids = [v['id'] for v in kwargs.get('volume_list')]

    task_input_class = None
    if task_input:
        task_input_class = SBGTaskInput(task_input['name'],
                                        app=task_input['project'] + '/' + app_name,
                                        project=task_input['project'],
                                        inputs=task_input['inputs'])

    # create data for sbg workflow run
    # create a sbg workflow run object to use
    wfrun = SBGWorkflowRun(token, project_id, app_name, task_id, task_input_class, import_id_list,
                           mounted_volume_ids, export_id_list,
                           kwargs.get('output_volume_id'), kwargs.get('export_report'))
    return wfrun


class SBGWorkflowRun(object):
    '''
    This class is mainly used to keep state information about our workflow run to be
    passed between the various lambda functions that orchestrate our workflow execution
    on sbg.
    '''

    base_url = "https://api.sbgenomics.com/v2"

    def __init__(self, token, project_id, app_name, task_id='', task_input=None,
                 import_id_list=None, mounted_volume_ids=None, export_id_list=None,
                 output_volume_id=None, export_report=None, header=None):

        # list of import ids for the files imported for the current run.
        self.import_id_list = [] if import_id_list is None else import_id_list
        mounted_volume_ids = [] if mounted_volume_ids is None else mounted_volume_ids
        self.export_id_list = [] if export_id_list is None else export_id_list
        self.export_report = [] if export_report is None else export_report
        self.output_volume_id = output_volume_id

        if not header:
            self.header = {"X-SBG-Auth-Token": token, "Content-type": "application/json"}
        else:
            self.header = header
        self.project_id = project_id
        self.app_name = app_name
        # list of volumes mounted for the current run. Helpful for deleting vols later
        self.volume_list = [create_sbg_volume_details(None, volume_id=id) for id in mounted_volume_ids]

        '''
        task_id for the current workflow run. It will be assigned after draft task
        is successfully created. We keep the information here, so we can re-run the
        task if it fails and also for the sanity check - so that we only run tasks that we created.
        '''
        self.task_id = task_id
        self.task_input = task_input  # SBGTaskInput object
        if task_input:
            # ensure task_input at least is for our project
            assert task_input.app == self.project_id + "/" + self.app_name
            assert task_input.project == self.project_id

    def as_dict(self):
        cleaned_workflow = self.__dict__.copy()
        cleaned_workflow.pop('header')
        ti = cleaned_workflow.get('task_input')
        if ti:
            cleaned_workflow['task_input'] = ti.as_dict()
        return cleaned_workflow

    def check_import(self, import_id):
        data = json.dumps({"import_id": import_id})
        import_url = self.base_url + "/storage/imports/" + import_id

        res = requests.get(import_url, headers=self.header)
        if res.status_code == 404:
            # maybe we have a file and not an import let's check
            file_url = '/files?project=%s&name=%s' % (self.project_id, import_id)
            fresp = requests.get(self.base_url + file_url, headers=self.header)

            if len(fresp.json().get('items', [])) >= 1:
                the_file = fresp.json()['items'][0]
                # we got a file, make it look like result from import completed
                data = {'result': {'name': the_file['name'], 'id': the_file['id']}}
            else:
                raise Exception("file not found")
        else:
            data = res.json()
            if data.get('state') != 'COMPLETED':
                raise SBGStillRunningException("file still uploading")

        return data

    def create_volumes(self, sbg_volume, bucket_name, public_key,
                       secret_key, bucket_object_prefix='', access_mode='rw'):
        '''
        function that creates a mounted volume on SBG
        sbg_volume:
        bucket_name: name of bucket to mount
        public_key, secret_key: keys for S3 bucket
        bucket_object_prefix : for subdirectory inside the bucket, use subdirectory_name+'/'
        access_mode : 'ro' for readonly 'rw' for read and write
        '''

        volume_url = self.base_url + "/storage/volumes/"
        data = {
            "name": sbg_volume['name'],
            "description": "some volume",
            "service": {
                 "type": "s3",
                 "bucket": bucket_name,
                 "prefix": bucket_object_prefix,
                 "credentials": {
                     "access_key_id": public_key,  # public access key for our s3 bucket
                     "secret_access_key": secret_key   # secret access key for our s3 bucket
                 },
                 "properties": {
                     "sse_algorithm": "AES256"
                 }
            },
            "access_mode": access_mode  # either 'rw' or 'ro'.
        }

        try:
            # first check and see if we have this volume already created
            response = requests.get(volume_url+sbg_volume['id'], headers=self.header)
            if response.status_code != 200:
                # if not create it
                response = requests.post(volume_url, headers=self.header, data=json.dumps(data))

            # sometimes sevenbridges services go down..x?
            if response.status_code == 503:
                raise Exception(response.json())
            # update volume_list
            if sbg_volume not in self.volume_list:
                self.volume_list.append(sbg_volume)
            return(response.json())
        except Exception as e:
            print(e)
            print("volume creation error")
            raise e

    def import_volume_content(self, sbg_volume, object_key, dest_upload_key=None):
        '''
        function that initiations importing (mounting) an object on 4dn s3 to SBG s3
        source_upload_key : object key on 4dn s3
        dest_upload_key : upload_key-to-be on SBG s3 (default, to be the same as source_upload_key)
        return value : the newly imported (mounted) file's ID on SBG S3
        '''
        if sbg_volume not in self.volume_list:
            raise Exception("Error: the specified volume doesn't exist in the current workflow run.")

        source_upload_key = object_key
        if dest_upload_key is None:
            dest_upload_key = object_key
        import_url = self.base_url + "/storage/imports"
        data = {
            "source": {
                "volume": sbg_volume['id'],
                "location": source_upload_key
            },
            "destination": {
                "project": self.project_id,
                "name": dest_upload_key
            },
            "overwrite": False
        }

        # TODO: problem here is there maybe no import id... so how to handle that
        # in check import?
        # first check and see if the file exists
        file_url = '/files?project=%s&name=%s' % (self.project_id, dest_upload_key)
        fresp = requests.get(self.base_url + file_url, headers=self.header)

        # this means our query returned  a file... so it's already imported
        if len(fresp.json().get('items', [])) >= 1:
            # just return a handle to the file
            import_id = fresp.json().get('items')[0]['name']
        else:
            # import a new one
            response = requests.post(import_url, headers=self.header, data=json.dumps(data))
            import_id = response.json().get('id')
        if import_id:
            if import_id not in self.import_id_list:
                self.import_id_list.append(import_id)
            return(import_id)
        else:
            raise Exception("Error: import not successful.")

    def create_task(self, sbg_task_input):
        '''
        create a draft task on SBG, given a SBGTaskInput object
        '''
        url = self.base_url + "/tasks"
        data = sbg_task_input.__dict__
        LOG.info('create task data sent to sbg is %s' % data)
        resp = requests.post(url, headers=self.header, data=json.dumps(data))

        if 'id' in resp.json().keys():
            self.task_id = resp.json().get('id')
            self.task_input = sbg_task_input
            return resp.json()
        else:
            raise Exception("task not created succesfully, resp is %s" % resp.json())

    def run_task(self):
        '''
        run task on SBG
        A draft task must be created before running it
        '''
        if self.task_id is None:
            raise Exception("Error: no task_id available. Create a draft task first.")

        url = self.base_url + "/tasks/" + self.task_id + "/actions/run"
        data = self.task_input.__dict__
        resp = requests.post(url, headers=self.header, data=json.dumps(data))
        return(resp.json())  # return the run_task response

    def check_task(self):
        '''
        check status of task
        '''
        if self.task_id is None:
            raise Exception("Error: no task_id available. Create a draft task first.")

        url = self.base_url + "/tasks/" + self.task_id
        data = {}
        response = requests.get(url, headers=self.header, data=json.dumps(data))
        return response.json()

    def create_data_payload_validatefiles(import_response):
        '''
        example method for creating sbgtaskinput for validate app given the response body of
        import request in json format
        '''
        try:
            file_id = import_response.get('result').get('id')  # imported Id on SBG
            file_name = import_response.get('result').get('name')  # imported file name on SBG
            # app_name = "validate"

            sbgtaskinput = None  # SBGTaskInput(self.project_id, app_name)
            sbgtaskinput.add_inputfile(file_name, file_id, "input_file")
            sbgtaskinput.add_inputparam("fastq", "type")

            return(sbgtaskinput)
        except Exception as e:
            print(e)
            print('Error creating a task payload')
            raise e

    # Initiate exporting all output files to S3 and returns an array of {upload_key, export_id} dictionary
    # export_id should be used to track export status.
    def export_all_output_files(self, sbg_run_detail_resp, ff_meta, base_dir="", sbg_volume=None):
        self.export_report = []
        self.export_id_list = []

        # all workflow runs must have an output file, either processed file, QC file or a report file.
        try:
            # workflow argument
            wodict = dict()
            for of in ff_meta.get('output_files'):
                wodict.update({of['workflow_argument_name']: {'type': of['type']}})
                if('value' in of):
                    wodict[of['workflow_argument_name']].update({'value': of['value']})
                if('upload_key' in of):
                    wodict[of['workflow_argument_name']].update({'upload_key': of['upload_key']})
                if('format' in of):
                    wodict[of['workflow_argument_name']].update({'format': of['format']})
                if('extension' in of):
                    wodict[of['workflow_argument_name']].update({'extension': of['extension']})

        except Exception as e:
            print("Can't create wodict out of output_files field of workflow_run metadata %s" % e)
            raise e

        if base_dir and not base_dir.endswith("/"):
            base_dir += "/"

        # get the output volume
        if sbg_volume is None:
            sbg_volume = self.output_volume_id

        # export all output files to s3
        for k, v in sbg_run_detail_resp.get('outputs').iteritems():
            if isinstance(v, dict) and v.get('class') == 'File':     # This is a file
                sbg_file_id = v['path'].encode('utf8')
                if wodict[k]['type'] == 'Output processed file':
                    # processed files
                    dest_upload_key = wodict[k]['upload_key']
                else:
                    # QC and report
                    # put all files in directory with uuid of workflowrun
                    dest_upload_key = "%s%s" % (base_dir, v['name'].encode('utf8'))
                export_id = self.export_to_volume(sbg_file_id, sbg_volume, dest_upload_key)
                # report will help with generating metadata later
                export_item = {"upload_key": dest_upload_key, "export_id": export_id, "workflow_argument_name": k}
                if 'value' in wodict[k]:
                    export_item.update({'value': wodict[k]['value']})
                self.export_report.append(export_item)
                self.export_id_list.append(export_id)
            elif isinstance(v, list):
                for v_el in v:
                    # This is a file (v is an array of files)
                    if isinstance(v_el, dict) and v_el.get('class') == 'File':
                        sbg_file_id = v['path'].encode('utf8')
                        if wodict[k]['type'] == 'Output processed file':
                            # processed files
                            # these files go into directory with file_uuid.
                            # Also change file name here (these files tend to be large)
                            dest_upload_key = wodict[k]['upload_key']
                        else:
                            # QC and report
                            # put all files in directory with uuid of workflowrun
                            dest_upload_key = "%s%s" % (base_dir, v['name'].encode('utf8'))
                        export_id = self.export_to_volume(sbg_file_id, sbg_volume, dest_upload_key,
                                                          extra={"aws_canned_acl": "public-read"})
                        export_item = {"upload_key": dest_upload_key,
                                       "export_id": export_id,
                                       "workflow_argument_name": k}
                        if 'value' in wodict[k]:
                            export_item.update({'value': wodict[k]['value']})
                        self.export_report.append(export_item)
                        self.export_id_list.append(export_id)
        return self.export_report

    def export_to_volume(self, source_file_id, sbg_volume_id, dest_upload_key, extra=None):
        '''
        This function exports a file on SBG to a mounted output bucket and returns export_id
        extra would be additional parameters to send to sbg
        '''

        export_url = self.base_url + "/storage/exports/?overwrite=true"
        data = {
            "source": {
                "file": source_file_id
            },
            "destination": {
                "volume": sbg_volume_id,
                "location": dest_upload_key
            }
        }
        if extra:
            data.merge(extra)
        response = requests.post(export_url, headers=self.header, data=json.dumps(data)).json()
        if 'id' in response.keys():
            # Export initiated corretly
            export_id = response.get('id')
            if export_id not in self.export_id_list:
                self.export_id_list.append(export_id)
            return export_id
        else:
            raise Exception("Export error %s \n request data is %s" % (response, data))

    def check_export(self, export_id):
        export_url = self.base_url + "/storage/exports/" + export_id
        data = {"export_id": export_id}
        response = requests.get(export_url, headers=self.header, data=json.dumps(data))
        return response.json()

    '''
    def delete_volumes(self):
        response_all=[]
        for sbg_volume in self.volume_list:
            url = self.base_url + "/storage/volumes/" + sbg_volume
            response = requests.delete(url, headers=self.header)
            response_all.append(response)
        self.volume_list=[]
        return({"responses": response_all})

    def delete_imported_files(self):
        response_all=[]
        for import_id in self.import_id_list:
            import_detail = self.get_details_of_import(import_id)
            imported_file_id = import_detail.get('result').get('id')
            url = self.base_url + "/storage/files/" + imported_file_id
            response = requests.delete(url, headers=self.header)
            response_all.append(response)
        self.import_id_list=[]
        return({"responses": response_all})


    def delete_exported_files(self):
        response_all=[]
        for export_id in self.export_id_list:
            export_detail = self.check_export(export_id)
            exported_file_id = export_detail.get('source').get('file')
            url = self.base_url + "/storage/files/" + exported_file_id
            response = requests.delete(url, headers=self.header)
            response_all.append(response)
        return({"responses": response_all})
    '''


class SBGTaskInput(object):
    def __init__(self, name, project, app='', inputs=None):
        self.name = name
        self.project = project
        if not app:
            self.app = project + "/" + name
        else:
            self.app = app
        if not inputs:
            self.inputs = {}
            for k, v in inputs.iteritems():
                if isinstance(k, (str, unicode)):
                    k = k.encode('utf-8')
                if isinstance(v, (str, unicode)):
                    v = v.encode('utf-8')
                self.add_inputparam(v, k)
        else:
            self.inputs = inputs

    def as_dict(self):
        return self.__dict__

    def add_input(self, new_input):
        self.inputs.update(new_input)

    def add_inputfile(self, upload_key, file_id, argument_name, is_list=False):
        '''
        create appropriate input for sbg task for the specified file.  Some inputs should
        be file arrays, i.e. a list of files attached to a single input argument_name.  so if
        is_list is true create those as a list, otherwise create the "standard" file input
        '''

        if is_list:
            input_list = ensure_list(self.inputs.get(argument_name, []))
            input_list.append({"class": "File", "name": upload_key, "path": file_id})
            new_input = {argument_name: input_list}
        else:
            new_input = {argument_name: {"class": "File", "name": upload_key, "path": file_id}}

        self.add_input(new_input)

    def add_inputparam(self, param_name, argument_name):
        new_input = {argument_name: param_name}
        self.add_input(new_input)

    def sbg2workflowrun(self, wr):
        wr.title = self.app_name + " run " + str(datetime.datetime.now())
        wr.sbg_task_id = self.task_id
        wr.sbg_mounted_volume_ids = []
        for x in self.volume_list:
            wr.sbg_mounted_volume_ids.append(x)
        wr.sbg_import_ids = self.import_id_list
        return (wr.__dict__)
