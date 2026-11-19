from worker.workflows.phrase_qa_voorvertaling import is_relevant_event


def test_relevant_event():
    assert is_relevant_event({"event": "JOB_STATUS_CHANGED", "jobParts": [{"status": "COMPLETED", "workflowLevel": 1}]})
    assert not is_relevant_event({"event": "PROJECT_CREATED"})
