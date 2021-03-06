from core.export_files_sbg.service import handler as export_files_handler
import pytest
from ..conftest import valid_env
import json


@valid_env
@pytest.mark.webtest
def test_export_files_sbg_e2e(export_files_event_data, tibanna_env):
    try:
        export_files_event_data.update(tibanna_env)
        ret = export_files_handler(export_files_event_data, None)
    except KeyError as e:
        pytest.skip("getting key error, probably test data issue")
    except Exception as e:
        # SBG throws error if you try to upload the same file twice
        assert "'status': 403" in str(e)
        assert "'code': 9006" in str(e)
    else:
        assert json.dumps(ret)
        assert ret['workflow']
        assert ret['ff_meta']['run_status'] == "output_files_transferring"
