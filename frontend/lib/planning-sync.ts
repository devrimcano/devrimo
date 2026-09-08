type Acknowledgment = { term: string; revision: number; idempotency_key?: string | null };

/** Reconcile request identities and revisions without overwriting newer local edits. */
export class PlanSaveTracker {
  private term = "";
  private revision = -1;
  private submissions = new Map<string, string>();

  reset(term: string) {
    this.term = term;
    this.revision = -1;
    this.submissions.clear();
  }

  submit(key: string, term: string, fingerprint: string) {
    if (term === this.term) this.submissions.set(key, fingerprint);
  }

  acknowledge(response: Acknowledgment, localFingerprint: string): "ignore" | "adopt" | "preserve" {
    if (response.term !== this.term || response.revision <= this.revision) return "ignore";
    this.revision = response.revision;
    const submitted = response.idempotency_key ? this.submissions.get(response.idempotency_key) : undefined;
    if (response.idempotency_key) this.submissions.delete(response.idempotency_key);
    return submitted === undefined || submitted === localFingerprint ? "adopt" : "preserve";
  }
}
