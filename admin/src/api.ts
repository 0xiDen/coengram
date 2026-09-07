import type {
  AdminData,
  AdminSession,
  AuditEvent,
  Credential,
  KnowledgeCandidate,
  Membership,
  Operator,
  OperatorTokenRecord,
  Principal,
  ProvisioningJob,
  RotatedCredential,
  Tenant,
  TokenRecord
} from "./types";

const CSRF_STORAGE_KEY = "coengram.admin.csrf";

type RequestOptions = {
  method?: string;
  body?: unknown;
  csrf?: string | null;
};

export class ApiError extends Error {
  readonly status: number;

  constructor(status: number, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

export function getStoredCsrf(): string | null {
  return window.sessionStorage.getItem(CSRF_STORAGE_KEY);
}

export function storeCsrf(csrf: string | null): void {
  if (csrf) {
    window.sessionStorage.setItem(CSRF_STORAGE_KEY, csrf);
    return;
  }
  window.sessionStorage.removeItem(CSRF_STORAGE_KEY);
}

async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const headers = new Headers();
  if (options.body !== undefined) {
    headers.set("Content-Type", "application/json");
  }
  if (options.csrf) {
    headers.set("X-CoEngram-CSRF", options.csrf);
  }
  const response = await fetch(`/api/v1/admin${path}`, {
    method: options.method ?? "GET",
    credentials: "include",
    headers,
    body: options.body === undefined ? undefined : JSON.stringify(options.body)
  });

  if (!response.ok) {
    let message = response.statusText || "Request failed";
    try {
      const body = (await response.json()) as { detail?: unknown };
      if (typeof body.detail === "string") {
        message = body.detail;
      }
    } catch {
      // Keep the HTTP status text.
    }
    throw new ApiError(response.status, message);
  }

  if (response.status === 204) {
    return undefined as T;
  }

  return (await response.json()) as T;
}

export async function login(accessToken: string): Promise<AdminSession> {
  const session = await request<AdminSession>("/session", {
    method: "POST",
    body: { access_token: accessToken }
  });
  storeCsrf(session.csrf_token ?? null);
  return session;
}

export async function currentSession(): Promise<AdminSession> {
  return request<AdminSession>("/session");
}

export async function logout(csrf: string | null): Promise<void> {
  await request<void>("/session", { method: "DELETE", csrf });
  storeCsrf(null);
}

export async function loadAdminData(): Promise<AdminData> {
  const [tenants, operators, principals, provisioningJobs, auditEvents] = await Promise.all([
    optional(request<{ tenants: Tenant[] }>("/tenants"), { tenants: [] }),
    optional(request<{ operators: Operator[] }>("/operators"), { operators: [] }),
    optional(request<{ principals: Principal[]; memberships: Membership[] }>("/principals"), {
      principals: [],
      memberships: []
    }),
    optional(request<{ jobs: ProvisioningJob[] }>("/provisioning-jobs"), { jobs: [] }),
    optional(request<{ events: AuditEvent[] }>("/audit-events"), { events: [] })
  ]);
  const knowledgeCandidates = await Promise.all(
    tenants.tenants.map(async (tenant) => {
      const result = await optional(
        request<{ candidates: KnowledgeCandidate[] }>(
          `/tenants/${encodeURIComponent(tenant.tenant_id)}/knowledge-candidates`
        ),
        { candidates: [] }
      );
      return result.candidates.map((candidate) => ({
        ...candidate,
        tenant_id: tenant.tenant_id
      }));
    })
  );

  return {
    tenants: tenants.tenants,
    operators: operators.operators,
    principals: principals.principals,
    memberships: principals.memberships,
    knowledgeCandidates: knowledgeCandidates.flat(),
    provisioningJobs: provisioningJobs.jobs,
    auditEvents: auditEvents.events
  };
}

async function optional<T>(promise: Promise<T>, fallback: T): Promise<T> {
  try {
    return await promise;
  } catch (exc) {
    if (exc instanceof ApiError && exc.status === 403) {
      return fallback;
    }
    throw exc;
  }
}

export async function createOperator(
  csrf: string | null,
  body: { operator_id: string; name: string; roles: string[] }
): Promise<Operator> {
  return request<Operator>("/operators", { method: "POST", csrf, body });
}

export async function updateOperator(
  csrf: string | null,
  operatorId: string,
  body: { name?: string; roles?: string[]; active?: boolean }
): Promise<Operator> {
  return request<Operator>(`/operators/${operatorId}`, { method: "PATCH", csrf, body });
}

export async function listOperatorTokens(operatorId: string): Promise<OperatorTokenRecord[]> {
  const result = await request<{ tokens: OperatorTokenRecord[] }>(`/operators/${operatorId}/tokens`);
  return result.tokens;
}

export async function issueOperatorToken(
  csrf: string | null,
  operatorId: string,
  lifetimeDays: number | null
): Promise<Credential> {
  return request<Credential>(`/operators/${operatorId}/tokens`, {
    method: "POST",
    csrf,
    body: { lifetime_days: lifetimeDays }
  });
}

export async function rotateOperatorToken(
  csrf: string | null,
  tokenId: string,
  overlapMinutes: number,
  lifetimeDays: number | null
): Promise<RotatedCredential> {
  return request<RotatedCredential>(`/operator-tokens/${tokenId}/rotate`, {
    method: "POST",
    csrf,
    body: { overlap_minutes: overlapMinutes, lifetime_days: lifetimeDays }
  });
}

export async function revokeOperatorToken(csrf: string | null, tokenId: string): Promise<void> {
  return request<void>(`/operator-tokens/${tokenId}`, { method: "DELETE", csrf });
}

export async function createUser(
  csrf: string | null,
  body: {
    tenant_id: string;
    principal_id: string;
    name: string;
    roles: string[];
    issue_token: boolean;
    token_lifetime_days: number | null;
  }
): Promise<{ principal: Principal; membership: Membership; credential: Credential | null }> {
  return request("/users", { method: "POST", csrf, body });
}

export async function issueToken(
  csrf: string | null,
  body: { tenant_id: string; principal_id: string; lifetime_days: number | null }
): Promise<Credential> {
  return request<Credential>("/tokens", { method: "POST", csrf, body });
}

export async function listTokens(tenantId: string, principalId: string): Promise<TokenRecord[]> {
  const search = new URLSearchParams({ tenant_id: tenantId, principal_id: principalId });
  const result = await request<{ tokens: TokenRecord[] }>(`/tokens?${search.toString()}`);
  return result.tokens;
}

export async function rotateToken(
  csrf: string | null,
  tokenId: string,
  overlapMinutes: number,
  lifetimeDays: number | null
): Promise<RotatedCredential> {
  return request<RotatedCredential>(`/tokens/${tokenId}/rotate`, {
    method: "POST",
    csrf,
    body: { overlap_minutes: overlapMinutes, lifetime_days: lifetimeDays }
  });
}

export async function revokeToken(csrf: string | null, tokenId: string): Promise<void> {
  return request<void>(`/tokens/${tokenId}`, { method: "DELETE", csrf });
}

export async function reviewKnowledgeCandidate(
  csrf: string | null,
  tenantId: string,
  candidateId: string,
  body: { decision: "approve" | "reject"; rationale: string; idempotency_key: string }
): Promise<KnowledgeCandidate> {
  return request<KnowledgeCandidate>(
    `/tenants/${encodeURIComponent(tenantId)}/knowledge-candidates/${encodeURIComponent(
      candidateId
    )}/reviews`,
    {
      method: "POST",
      csrf,
      body
    }
  );
}

export async function createProvisioningJob(
  csrf: string | null,
  body: { idempotency_key: string; manifest: unknown }
): Promise<ProvisioningJob> {
  return request<ProvisioningJob>("/provisioning-jobs", { method: "POST", csrf, body });
}

export async function cancelProvisioningJob(
  csrf: string | null,
  jobId: string
): Promise<ProvisioningJob> {
  return request<ProvisioningJob>(`/provisioning-jobs/${jobId}/cancel`, {
    method: "POST",
    csrf
  });
}
