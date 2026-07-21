"""Pipeline orchestration services -- the assess/onboard background-job
logic, split out of ``routes/assessments.py`` (2026-07-20 reuse/refactor
review: that file had grown to ~2000 lines mixing HTTP route handlers with
the actual assess->onboard->deliver pipeline orchestration).

**Layout:**

- ``assess_pipeline.py`` -- everything downstream of ``POST /assess``:
  resolving/validating the mandatory GitOps infra repo, the clone+assess
  work itself, and ``start_assess_job()``, which owns the exact
  threading/event-loop-bridging pattern ``assess_submit()`` used to run
  inline (a plain ``threading.Thread`` whose body schedules every store
  call back onto the request's event loop via
  ``asyncio.run_coroutine_threadsafe`` -- see that function's own
  docstring for why this is the trickiest part of the whole split to get
  right).
- ``onboard_pipeline.py`` -- everything downstream of
  ``POST /assessments/{id}/onboard`` (and the onboard leg of the
  webhook-triggered assess->onboard chain, and the manual "Run Automatic
  Validation" retry): running orchestration, then the automatic
  validate -> fix -> final-review -> real-Deliver pipeline
  (``auto_delivery.py``), all FastAPI ``BackgroundTasks``-driven rather
  than thread-based (this leg never needs the thread/loop bridge above --
  ``BackgroundTasks`` already runs on the same event loop that started
  it).

``routes/assessments.py`` still owns every real ``@router.post``/
``@router.get`` handler -- it imports from these modules and stays a thin
wrapper (request parsing + calling into the pipeline + building the HTTP
response), never re-implementing the orchestration itself. See that
module's own docstring for the full list of what stayed there and why
(mostly: the view-level "where is this job on the unified progress
stepper" / terminal-redirect-URL helpers, which are response-building
logic, not pipeline orchestration).
"""
