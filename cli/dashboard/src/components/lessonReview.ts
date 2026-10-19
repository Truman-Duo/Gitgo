export type ProposalRef = { proposal_id: string; proposal_version: string; candidate_index: number };
export type CandidateReview = { index: number; action: "accept" | "discard"; trigger: string; rule: string; qualification: Record<string, string> };
export const EDIT_FIELDS = ["trigger", "rule", "knowledge_type", "conditions", "validation", "novelty", "limitations"] as const;
export function proposalReviews(proposal: any): CandidateReview[] {
  return (proposal.candidates || []).map((candidate: any, index: number) => ({
    index, action: "accept", trigger: String(candidate.trigger || ""), rule: String(candidate.rule || ""),
    qualification: {...candidate.qualification},
  }));
}
export function fieldValue(review: CandidateReview, field: typeof EDIT_FIELDS[number]): string {
  return field === "trigger" || field === "rule" ? review[field] : review.qualification[field] || "";
}
export function reviseField(review: CandidateReview, field: typeof EDIT_FIELDS[number], value: string): CandidateReview {
  const clean = value.trim();
  const minimum = field === "rule" ? 20 : 1;
  if (clean.length < minimum || clean.length > (field === "trigger" ? 240 : 2000)) throw new Error("This field is empty, too short or too long.");
  if (field === "knowledge_type" && !["compatibility", "failure_recovery", "project_constraint", "procedure"].includes(clean)) throw new Error("Choose compatibility, failure_recovery, project_constraint or procedure.");
  return field === "trigger" || field === "rule" ? {...review, [field]: clean}
    : {...review, qualification: {...review.qualification, [field]: clean}};
}
export function reviewsForSubmit(reviews: CandidateReview[]): any[] {
  return reviews.map(review => review.action === "discard" ? {index: review.index, action: "discard"} : {...review});
}
