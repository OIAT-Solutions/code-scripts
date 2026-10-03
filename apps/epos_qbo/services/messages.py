"""Business wording for recorded outcomes. A finished process is not a posted sale."""

STEP_OUTCOMES = {
    "ok": ("Finished", "This step finished. Check the recorded results below.", "neutral"),
    "review": ("Needs review", "Some records need a person to check them.", "warning"),
    "failed": ("Could not finish", "Open the supporting details before trying again.", "danger"),
    "skipped": ("Not run", "This step was skipped in this attempt.", "neutral"),
    "disabled": ("Turned off", "This step was turned off in this attempt.", "neutral"),
}


def step_outcome(step, preview=False):
    label, sentence, tone = STEP_OUTCOMES.get(
        step.status, ("Not confirmed", "There is no confirmed outcome for this step.", "neutral")
    )
    if preview:
        sentence = "This was a preview. Nothing was posted by this attempt. " + sentence
        if step.status == "ok":
            label = "Preview ready"
    return dict(label=label, sentence=sentence, tone=tone)


def job_outcome(job, artifacts):
    from ..models import RunJob
    from .experience import confirmed_artifact, job_message

    confirmed = any(confirmed_artifact(a) for a in artifacts)
    label, sentence, tone = job_message(job, confirmed)
    if job.scope in (RunJob.SCOPE_INVENTORY_PIPELINE, RunJob.SCOPE_INVENTORY_SYNC, RunJob.SCOPE_PORTAL_REVIEW):
        if job.status == RunJob.STATUS_SUCCEEDED:
            label, sentence, tone = "Finished", "This work finished. Review its recorded result below.", "neutral"
    return dict(label=label, sentence=sentence, tone=tone)
