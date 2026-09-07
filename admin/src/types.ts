export type AdminSession = {
  session_id: string;
  operator_id: string;
  roles: string[];
  csrf_token?: string;
  absolute_expires_at?: string;
  idle_expires_at?: string;
};

export type Tenant = {
  tenant_id: string;
  name: string;
  active: boolean;
};

export type Operator = {
  operator_id: string;
  name: string;
  roles: string[];
  active: boolean;
};

export type Principal = {
  principal_id: string;
  name: string;
  kind: string;
  active: boolean;
};

export type Membership = {
  tenant_id: string;
  principal_id: string;
  roles: string[];
  active: boolean;
};

export type Credential = {
  token_id: string;
  access_token: string;
  expires_at: string;
  warning: string;
};

export type RotatedCredential = {
  previous_token_id: string;
  previous_valid_until: string;
  credential: Credential;
};

export type TokenRecord = {
  token_id: string;
  tenant_id: string;
  principal_id: string;
  actor_kind: string;
  roles: string[];
  subject_user_id: string | null;
  delegation_id: string | null;
  issued_at: string;
  expires_at: string;
  revoked_at: string | null;
  last_used_at: string | null;
  active: boolean;
};

export type OperatorTokenRecord = {
  token_id: string;
  operator_id: string;
  roles: string[];
  issued_at: string;
  expires_at: string;
  revoked_at: string | null;
  last_used_at: string | null;
  active: boolean;
};

export type ProvisioningJob = {
  job_id: string;
  tenant_id: string;
  manifest_fingerprint: string;
  requested_by_operator_id: string;
  state: string;
  attempt: number;
  completed_steps: string[];
  failed_step: string | null;
  failure_code: string | null;
  claimed_by: string | null;
  claimed_at: string | null;
  heartbeat_at: string | null;
  cancel_requested_at: string | null;
  cleanup_requested_at: string | null;
  cleanup_completed_at: string | null;
  created_at: string;
  updated_at: string;
};

export type KnowledgeCandidate = {
  id: string;
  tenant_id?: string;
  claim: string;
  confidence: number;
  proposer_id: string;
  source_count: number;
  duplicate_memory_ids: string[];
  conflicting_memory_ids: string[];
  status: string;
  created_at: string;
  reviewed_by: string | null;
  review_rationale: string | null;
};

export type PrivateMemoryMetadata = {
  memory_id: string;
  owner_principal_id: string;
  state: string;
  operation_id: string | null;
  mutation_state: string;
  kind: string | null;
  confidence: number | null;
  created_at: string;
  supersedes_id: string | null;
};

export type TenantKnowledgeItem = {
  memory_id: string;
  content: string;
  kind: string;
  confidence: number;
  provenance_actor_id: string;
  provenance_source: string;
  created_at: string;
};

export type KnowledgeGraphNode = {
  node_id: string;
  node_type: string;
  label: string;
  metadata: Record<string, string>;
};

export type KnowledgeGraphEdge = {
  source_id: string;
  target_id: string;
  label: string;
};

export type KnowledgeGraph = {
  nodes: KnowledgeGraphNode[];
  edges: KnowledgeGraphEdge[];
};

export type AuditEvent = {
  event_id: string;
  operator_id: string;
  roles: string[];
  action: string;
  target_type: string;
  target_ids: Record<string, string>;
  outcome: string;
  request_ref: string | null;
  before_metadata: Record<string, string>;
  after_metadata: Record<string, string>;
  created_at: string;
};

export type AdminData = {
  tenants: Tenant[];
  operators: Operator[];
  principals: Principal[];
  memberships: Membership[];
  knowledgeCandidates: KnowledgeCandidate[];
  provisioningJobs: ProvisioningJob[];
  auditEvents: AuditEvent[];
};
