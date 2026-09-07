import {
  Alert,
  AlertIcon,
  Badge,
  Box,
  Button,
  Checkbox,
  CheckboxGroup,
  Code,
  Divider,
  Flex,
  FormControl,
  FormLabel,
  Grid,
  GridItem,
  HStack,
  Heading,
  IconButton,
  Input,
  Select,
  SimpleGrid,
  Spacer,
  Spinner,
  Stack,
  Stat,
  StatHelpText,
  StatLabel,
  StatNumber,
  Switch,
  Table,
  TableContainer,
  Tbody,
  Td,
  Textarea,
  Text,
  Th,
  Thead,
  Tooltip,
  Tr,
  VStack,
  useToast
} from "@chakra-ui/react";
import {
  Activity,
  Ban,
  BrainCircuit,
  Building2,
  CheckCircle2,
  ClipboardList,
  Copy,
  KeyRound,
  LayoutDashboard,
  LogOut,
  Plus,
  RefreshCw,
  RotateCcw,
  ScrollText,
  ShieldCheck,
  Users,
  XCircle
} from "lucide-react";
import { FormEvent, ReactNode, useEffect, useMemo, useState } from "react";

import {
  ApiError,
  cancelProvisioningJob,
  createOperator,
  createProvisioningJob,
  createUser,
  currentSession,
  getStoredCsrf,
  issueOperatorToken,
  issueToken,
  listOperatorTokens,
  listTokens,
  loadAdminData,
  login,
  logout,
  reviewKnowledgeCandidate,
  revokeOperatorToken,
  revokeToken,
  rotateOperatorToken,
  rotateToken,
  storeCsrf,
  updateOperator
} from "./api";
import type {
  AdminData,
  AdminSession,
  Credential,
  Operator,
  OperatorTokenRecord,
  ProvisioningJob,
  RotatedCredential,
  TokenRecord
} from "./types";

type PageKey =
  | "dashboard"
  | "tenants"
  | "knowledge"
  | "identity"
  | "tokens"
  | "operators"
  | "provisioning"
  | "audit";

const NAV_ITEMS: Array<{ key: PageKey; label: string; icon: JSX.Element }> = [
  { key: "dashboard", label: "Dashboard", icon: <LayoutDashboard size={18} /> },
  { key: "tenants", label: "Tenants", icon: <Building2 size={18} /> },
  { key: "knowledge", label: "Knowledge", icon: <BrainCircuit size={18} /> },
  { key: "identity", label: "Identity", icon: <Users size={18} /> },
  { key: "tokens", label: "Tokens", icon: <KeyRound size={18} /> },
  { key: "operators", label: "Operators", icon: <ShieldCheck size={18} /> },
  { key: "provisioning", label: "Jobs", icon: <ClipboardList size={18} /> },
  { key: "audit", label: "Audit", icon: <ScrollText size={18} /> }
];

const EMPTY_DATA: AdminData = {
  tenants: [],
  operators: [],
  principals: [],
  memberships: [],
  knowledgeCandidates: [],
  provisioningJobs: [],
  auditEvents: []
};

const OPERATOR_ROLES = [
  "operator_admin",
  "identity_admin",
  "tenant_provisioner",
  "tenant_support",
  "knowledge_admin",
  "token_admin",
  "audit_viewer"
];

const TENANT_ROLES = ["tenant_administrator", "knowledge_curator", "tenant_member"];

export default function App() {
  const [session, setSession] = useState<AdminSession | null>(null);
  const [csrf, setCsrf] = useState<string | null>(() => getStoredCsrf());
  const [checkingSession, setCheckingSession] = useState(true);

  useEffect(() => {
    let alive = true;
    currentSession()
      .then((current) => {
        if (alive) {
          setSession({ ...current, csrf_token: csrf ?? undefined });
        }
      })
      .catch(() => {
        if (alive) {
          setSession(null);
          storeCsrf(null);
          setCsrf(null);
        }
      })
      .finally(() => {
        if (alive) {
          setCheckingSession(false);
        }
      });
    return () => {
      alive = false;
    };
  }, [csrf]);

  if (checkingSession) {
    return (
      <Flex minH="100vh" align="center" justify="center">
        <Spinner color="brand.500" size="xl" thickness="4px" />
      </Flex>
    );
  }

  if (!session) {
    return (
      <LoginView
        onLogin={(created) => {
          const nextCsrf = created.csrf_token ?? null;
          storeCsrf(nextCsrf);
          setCsrf(nextCsrf);
          setSession(created);
        }}
      />
    );
  }

  return (
    <AdminShell
      csrf={csrf}
      session={session}
      onCsrfChange={setCsrf}
      onLogout={() => setSession(null)}
    />
  );
}

function LoginView({ onLogin }: { onLogin: (session: AdminSession) => void }) {
  const [token, setToken] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      onLogin(await login(token));
    } catch (exc) {
      setError(errorMessage(exc));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Flex minH="100vh" align="center" justify="center" px={4} bg="ink.50">
      <Box
        as="form"
        onSubmit={submit}
        width="full"
        maxW="420px"
        bg="white"
        border="1px solid"
        borderColor="ink.100"
        borderRadius="8px"
        boxShadow="0 24px 70px rgba(17, 24, 23, 0.10)"
        p={8}
      >
        <VStack align="stretch" spacing={6}>
          <Box>
            <HStack spacing={3} mb={4}>
              <Flex
                align="center"
                justify="center"
                boxSize="42px"
                borderRadius="8px"
                bg="brand.600"
                color="white"
              >
                <ShieldCheck size={22} />
              </Flex>
              <Box>
                <Heading size="md">CoEngram Admin</Heading>
                <Text color="ink.500" fontSize="sm">
                  Operator access
                </Text>
              </Box>
            </HStack>
          </Box>
          {error ? (
            <Alert status="error" borderRadius="8px">
              <AlertIcon />
              <Text fontSize="sm">{error}</Text>
            </Alert>
          ) : null}
          <FormControl>
            <FormLabel>Operator Access Token</FormLabel>
            <Input
              autoFocus
              value={token}
              onChange={(event) => setToken(event.target.value)}
              type="password"
              placeholder="op1..."
            />
          </FormControl>
          <Button type="submit" isLoading={busy} leftIcon={<KeyRound size={16} />}>
            Sign in
          </Button>
        </VStack>
      </Box>
    </Flex>
  );
}

function AdminShell({
  session,
  csrf,
  onCsrfChange,
  onLogout
}: {
  session: AdminSession;
  csrf: string | null;
  onCsrfChange: (csrf: string | null) => void;
  onLogout: () => void;
}) {
  const toast = useToast();
  const [page, setPage] = useState<PageKey>("dashboard");
  const [data, setData] = useState<AdminData>(EMPTY_DATA);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [credential, setCredential] = useState<Credential | RotatedCredential | null>(null);

  async function refresh() {
    setLoading(true);
    setError(null);
    try {
      setData(await loadAdminData());
    } catch (exc) {
      setError(errorMessage(exc));
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    void refresh();
  }, []);

  async function signOut() {
    try {
      await logout(csrf);
    } catch {
      storeCsrf(null);
    }
    onCsrfChange(null);
    onLogout();
  }

  async function copy(value: string) {
    await navigator.clipboard.writeText(value);
    toast({ status: "success", title: "Copied", duration: 1800 });
  }

  const pageContent = (
    <PageBoundary loading={loading} error={error}>
      {page === "dashboard" ? <DashboardPage data={data} /> : null}
      {page === "tenants" ? (
        <TenantsPage csrf={csrf} data={data} onRefresh={refresh} onCredential={setCredential} />
      ) : null}
      {page === "knowledge" ? (
        <KnowledgePage csrf={csrf} data={data} onRefresh={refresh} />
      ) : null}
      {page === "identity" ? (
        <IdentityPage csrf={csrf} data={data} onRefresh={refresh} onCredential={setCredential} />
      ) : null}
      {page === "tokens" ? (
        <TokensPage csrf={csrf} data={data} onCredential={setCredential} onRefresh={refresh} />
      ) : null}
      {page === "operators" ? (
        <OperatorsPage csrf={csrf} data={data} onCredential={setCredential} onRefresh={refresh} />
      ) : null}
      {page === "provisioning" ? (
        <ProvisioningPage csrf={csrf} data={data} onRefresh={refresh} />
      ) : null}
      {page === "audit" ? <AuditPage data={data} /> : null}
    </PageBoundary>
  );

  return (
    <Flex minH="100vh" bg="ink.50">
      <Sidebar page={page} onPageChange={setPage} />
      <Box flex="1" minW={0}>
        <Flex
          as="header"
          align="center"
          gap={4}
          px={{ base: 4, md: 8 }}
          py={4}
          bg="white"
          borderBottom="1px solid"
          borderColor="ink.100"
          position="sticky"
          top={0}
          zIndex={5}
        >
          <Select
            display={{ base: "block", lg: "none" }}
            value={page}
            maxW="190px"
            onChange={(event) => setPage(event.target.value as PageKey)}
          >
            {NAV_ITEMS.map((item) => (
              <option key={item.key} value={item.key}>
                {item.label}
              </option>
            ))}
          </Select>
          <Box minW={0}>
            <Heading size="md">{NAV_ITEMS.find((item) => item.key === page)?.label}</Heading>
            <Text color="ink.500" fontSize="sm" noOfLines={1}>
              {session.operator_id}
            </Text>
          </Box>
          <Spacer />
          <Tooltip label="Refresh">
            <IconButton
              aria-label="Refresh"
              icon={<RefreshCw size={16} />}
              variant="outline"
              colorScheme="gray"
              onClick={refresh}
            />
          </Tooltip>
          <Button variant="outline" colorScheme="gray" leftIcon={<LogOut size={16} />} onClick={signOut}>
            Sign out
          </Button>
        </Flex>
        <Box px={{ base: 4, md: 8 }} py={6}>
          <CredentialBanner credential={credential} onCopy={copy} onClose={() => setCredential(null)} />
          {pageContent}
        </Box>
      </Box>
    </Flex>
  );
}

function Sidebar({ page, onPageChange }: { page: PageKey; onPageChange: (page: PageKey) => void }) {
  return (
    <VStack
      display={{ base: "none", lg: "flex" }}
      align="stretch"
      spacing={4}
      w="260px"
      minH="100vh"
      bg="ink.900"
      color="white"
      px={4}
      py={5}
      position="sticky"
      top={0}
    >
      <HStack spacing={3} px={2}>
        <Flex align="center" justify="center" boxSize="38px" borderRadius="8px" bg="brand.400">
          <Activity size={20} />
        </Flex>
        <Box>
          <Heading size="sm">CoEngram</Heading>
          <Text fontSize="xs" color="whiteAlpha.700">
            Admin plane
          </Text>
        </Box>
      </HStack>
      <Divider borderColor="whiteAlpha.200" />
      <VStack align="stretch" spacing={1}>
        {NAV_ITEMS.map((item) => {
          const selected = page === item.key;
          return (
            <Button
              key={item.key}
              justifyContent="flex-start"
              leftIcon={item.icon}
              variant={selected ? "solid" : "ghost"}
              bg={selected ? "white" : "transparent"}
              color={selected ? "ink.900" : "whiteAlpha.850"}
              colorScheme={selected ? "gray" : "whiteAlpha"}
              _hover={{ bg: selected ? "white" : "whiteAlpha.200" }}
              onClick={() => onPageChange(item.key)}
            >
              {item.label}
            </Button>
          );
        })}
      </VStack>
    </VStack>
  );
}

function PageBoundary({
  loading,
  error,
  children
}: {
  loading: boolean;
  error: string | null;
  children: ReactNode;
}) {
  if (loading) {
    return (
      <Flex align="center" justify="center" minH="420px">
        <Spinner color="brand.500" size="lg" />
      </Flex>
    );
  }
  if (error) {
    return (
      <Alert status="error" borderRadius="8px">
        <AlertIcon />
        <Text>{error}</Text>
      </Alert>
    );
  }
  return <>{children}</>;
}

function DashboardPage({ data }: { data: AdminData }) {
  const expiringOperators = data.operators.filter((operator) => operator.active).length;
  const queue = data.provisioningJobs.filter((job) =>
    ["queued", "running", "failed", "cancel_requested"].includes(job.state)
  );
  const pendingCandidates = data.knowledgeCandidates.filter(
    (candidate) => candidate.status === "submitted"
  ).length;

  return (
    <VStack align="stretch" spacing={6}>
      <SimpleGrid columns={{ base: 1, md: 2, xl: 4 }} spacing={4}>
        <StatCard label="Tenants" value={data.tenants.length} helper={`${activeCount(data.tenants)} active`} />
        <StatCard label="Operators" value={data.operators.length} helper={`${expiringOperators} active`} />
        <StatCard label="Principals" value={data.principals.length} helper={`${activeCount(data.principals)} active`} />
        <StatCard label="Knowledge" value={pendingCandidates} helper="pending review" />
      </SimpleGrid>
      <Panel title="Action Queue" icon={<ClipboardList size={18} />}>
        <TableView
          headers={["Tenant", "State", "Attempt", "Updated"]}
          empty="No active Provisioning Jobs"
          rows={queue.slice(0, 8).map((job) => [
            <Code key="tenant">{job.tenant_id}</Code>,
            <StatusBadge key="state" value={job.state} />,
            job.attempt,
            formatDate(job.updated_at)
          ])}
        />
      </Panel>
      <Panel title="Recent Audit" icon={<ScrollText size={18} />}>
        <TableView
          headers={["Action", "Actor", "Target", "When"]}
          empty="No Operator Audit Events"
          rows={data.auditEvents.slice(0, 8).map((event) => [
            event.action,
            <Code key="actor">{event.operator_id}</Code>,
            event.target_type,
            formatDate(event.created_at)
          ])}
        />
      </Panel>
    </VStack>
  );
}

function TenantsPage({
  csrf,
  data,
  onRefresh,
  onCredential
}: {
  csrf: string | null;
  data: AdminData;
  onRefresh: () => Promise<void>;
  onCredential: (credential: Credential | RotatedCredential | null) => void;
}) {
  const toast = useToast();
  const [tenantId, setTenantId] = useState("");
  const [name, setName] = useState("");
  const [adminPrincipalId, setAdminPrincipalId] = useState("");
  const [adminName, setAdminName] = useState("");
  const [idempotencyKey, setIdempotencyKey] = useState("");
  const [busy, setBusy] = useState(false);

  async function submit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    try {
      const job = await createProvisioningJob(csrf, {
        idempotency_key: idempotencyKey || `${tenantId}-initial`,
        manifest: {
          version: 1,
          tenant_id: tenantId,
          name,
          principals: [
            {
              principal_id: adminPrincipalId,
              name: adminName,
              kind: "user"
            }
          ],
          memberships: [
            {
              principal_id: adminPrincipalId,
              roles: ["tenant_administrator"]
            }
          ]
        }
      });
      onCredential(null);
      toast({ status: "success", title: `Provisioning Job ${shortId(job.job_id)} queued` });
      setTenantId("");
      setName("");
      setAdminPrincipalId("");
      setAdminName("");
      setIdempotencyKey("");
      await onRefresh();
    } catch (exc) {
      toast({ status: "error", title: errorMessage(exc) });
    } finally {
      setBusy(false);
    }
  }

  return (
    <Grid templateColumns={{ base: "1fr", xl: "minmax(0, 1fr) 380px" }} gap={5}>
      <GridItem>
        <Panel title="Tenants" icon={<Building2 size={18} />}>
          <TableView
            headers={["Tenant", "Name", "State"]}
            empty="No Tenants"
            rows={data.tenants.map((tenant) => [
              <Code key="tenant">{tenant.tenant_id}</Code>,
              tenant.name,
              <Badge key="state" colorScheme={tenant.active ? "green" : "gray"}>
                {tenant.active ? "active" : "inactive"}
              </Badge>
            ])}
          />
        </Panel>
      </GridItem>
      <GridItem>
        <Panel title="New Tenant" icon={<Plus size={18} />}>
          <Stack as="form" spacing={4} onSubmit={submit}>
            <FormControl isRequired>
              <FormLabel>Tenant ID</FormLabel>
              <Input value={tenantId} onChange={(event) => setTenantId(event.target.value)} />
            </FormControl>
            <FormControl isRequired>
              <FormLabel>Name</FormLabel>
              <Input value={name} onChange={(event) => setName(event.target.value)} />
            </FormControl>
            <FormControl isRequired>
              <FormLabel>Admin Principal ID</FormLabel>
              <Input value={adminPrincipalId} onChange={(event) => setAdminPrincipalId(event.target.value)} />
            </FormControl>
            <FormControl isRequired>
              <FormLabel>Admin Name</FormLabel>
              <Input value={adminName} onChange={(event) => setAdminName(event.target.value)} />
            </FormControl>
            <FormControl>
              <FormLabel>Idempotency Key</FormLabel>
              <Input value={idempotencyKey} onChange={(event) => setIdempotencyKey(event.target.value)} />
            </FormControl>
            <Button type="submit" isLoading={busy} leftIcon={<Plus size={16} />}>
              Queue Job
            </Button>
          </Stack>
        </Panel>
      </GridItem>
    </Grid>
  );
}

function KnowledgePage({
  csrf,
  data,
  onRefresh
}: {
  csrf: string | null;
  data: AdminData;
  onRefresh: () => Promise<void>;
}) {
  const toast = useToast();
  const [tenantId, setTenantId] = useState(data.tenants[0]?.tenant_id ?? "");
  const [selectedId, setSelectedId] = useState("");
  const [rationale, setRationale] = useState("Reviewed from Operator admin panel.");
  const [busyDecision, setBusyDecision] = useState<"approve" | "reject" | null>(null);

  useEffect(() => {
    if (!tenantId && data.tenants[0]) {
      setTenantId(data.tenants[0].tenant_id);
    }
  }, [data.tenants, tenantId]);

  const candidates = useMemo(
    () =>
      data.knowledgeCandidates.filter((candidate) => candidate.tenant_id === tenantId),
    [data.knowledgeCandidates, tenantId]
  );

  useEffect(() => {
    if (!candidates.some((candidate) => candidate.id === selectedId)) {
      setSelectedId(candidates[0]?.id ?? "");
    }
  }, [candidates, selectedId]);

  const selected = candidates.find((candidate) => candidate.id === selectedId) ?? null;

  async function review(decision: "approve" | "reject") {
    if (!selected || !tenantId) {
      return;
    }
    setBusyDecision(decision);
    try {
      const reviewed = await reviewKnowledgeCandidate(csrf, tenantId, selected.id, {
        decision,
        rationale,
        idempotency_key: idempotencyKey(`knowledge-${decision}`)
      });
      toast({
        status: "success",
        title: `${shortId(reviewed.id)} ${reviewed.status}`
      });
      await onRefresh();
    } catch (exc) {
      toast({ status: "error", title: errorMessage(exc) });
    } finally {
      setBusyDecision(null);
    }
  }

  return (
    <Grid templateColumns={{ base: "1fr", xl: "minmax(0, 1fr) 420px" }} gap={5}>
      <GridItem>
        <Panel title="Knowledge Candidates" icon={<BrainCircuit size={18} />}>
          <SimpleGrid columns={{ base: 1, md: 3 }} spacing={4} mb={5}>
            <FormControl>
              <FormLabel>Tenant</FormLabel>
              <Select value={tenantId} onChange={(event) => setTenantId(event.target.value)}>
                {data.tenants.map((tenant) => (
                  <option key={tenant.tenant_id} value={tenant.tenant_id}>
                    {tenant.name}
                  </option>
                ))}
              </Select>
            </FormControl>
            <Stat>
              <StatLabel color="ink.500">Submitted</StatLabel>
              <StatNumber fontSize="2xl">
                {candidates.filter((candidate) => candidate.status === "submitted").length}
              </StatNumber>
            </Stat>
            <Stat>
              <StatLabel color="ink.500">Total</StatLabel>
              <StatNumber fontSize="2xl">{candidates.length}</StatNumber>
            </Stat>
          </SimpleGrid>
          <TableView
            headers={["Candidate", "Status", "Confidence", "Proposer", "Sources", ""]}
            empty="No Knowledge Candidates"
            rows={candidates.map((candidate) => [
              <Text key="claim" noOfLines={2} maxW="520px">
                {candidate.claim}
              </Text>,
              <StatusBadge key="status" value={candidate.status} />,
              `${Math.round(candidate.confidence * 100)}%`,
              <Code key="proposer">{shortId(candidate.proposer_id)}</Code>,
              candidate.source_count,
              <HStack key="actions" justify="end">
                <Button
                  size="sm"
                  variant={candidate.id === selectedId ? "solid" : "outline"}
                  leftIcon={<BrainCircuit size={14} />}
                  onClick={() => setSelectedId(candidate.id)}
                >
                  Review
                </Button>
              </HStack>
            ])}
          />
        </Panel>
      </GridItem>
      <GridItem>
        <Panel title="Review" icon={<CheckCircle2 size={18} />}>
          {selected ? (
            <Stack spacing={4}>
              <Box>
                <Text fontSize="xs" color="ink.500" mb={1}>
                  Candidate
                </Text>
                <Code>{shortId(selected.id)}</Code>
              </Box>
              <Box>
                <Text fontSize="xs" color="ink.500" mb={1}>
                  Claim
                </Text>
                <Text fontWeight={600}>{selected.claim}</Text>
              </Box>
              <SimpleGrid columns={2} spacing={3}>
                <Box>
                  <Text fontSize="xs" color="ink.500">
                    Status
                  </Text>
                  <StatusBadge value={selected.status} />
                </Box>
                <Box>
                  <Text fontSize="xs" color="ink.500">
                    Created
                  </Text>
                  <Text fontSize="sm">{formatDate(selected.created_at)}</Text>
                </Box>
                <Box>
                  <Text fontSize="xs" color="ink.500">
                    Duplicates
                  </Text>
                  <Text fontSize="sm">{selected.duplicate_memory_ids.length}</Text>
                </Box>
                <Box>
                  <Text fontSize="xs" color="ink.500">
                    Conflicts
                  </Text>
                  <Text fontSize="sm">{selected.conflicting_memory_ids.length}</Text>
                </Box>
              </SimpleGrid>
              {selected.reviewed_by ? (
                <Alert status="info" borderRadius="8px">
                  <AlertIcon />
                  <Text fontSize="sm">Reviewed by {selected.reviewed_by}</Text>
                </Alert>
              ) : null}
              <FormControl isDisabled={selected.status !== "submitted"}>
                <FormLabel>Rationale</FormLabel>
                <Textarea
                  minH="130px"
                  resize="vertical"
                  value={rationale}
                  onChange={(event) => setRationale(event.target.value)}
                />
              </FormControl>
              <HStack justify="end">
                <Button
                  leftIcon={<XCircle size={16} />}
                  colorScheme="red"
                  variant="outline"
                  isDisabled={selected.status !== "submitted"}
                  isLoading={busyDecision === "reject"}
                  onClick={() => review("reject")}
                >
                  Reject
                </Button>
                <Button
                  leftIcon={<CheckCircle2 size={16} />}
                  isDisabled={selected.status !== "submitted"}
                  isLoading={busyDecision === "approve"}
                  onClick={() => review("approve")}
                >
                  Approve
                </Button>
              </HStack>
            </Stack>
          ) : (
            <Flex align="center" justify="center" minH="260px">
              <Text color="ink.500">No candidate selected</Text>
            </Flex>
          )}
        </Panel>
      </GridItem>
    </Grid>
  );
}

function IdentityPage({
  csrf,
  data,
  onRefresh,
  onCredential
}: {
  csrf: string | null;
  data: AdminData;
  onRefresh: () => Promise<void>;
  onCredential: (credential: Credential | RotatedCredential | null) => void;
}) {
  const toast = useToast();
  const [tenantId, setTenantId] = useState(data.tenants[0]?.tenant_id ?? "");
  const [principalId, setPrincipalId] = useState("");
  const [name, setName] = useState("");
  const [roles, setRoles] = useState<string[]>(["tenant_member"]);
  const [issue, setIssue] = useState(false);
  const [lifetime, setLifetime] = useState(30);
  const [busy, setBusy] = useState(false);

  async function submit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    try {
      const created = await createUser(csrf, {
        tenant_id: tenantId,
        principal_id: principalId,
        name,
        roles,
        issue_token: issue,
        token_lifetime_days: issue ? lifetime : null
      });
      onCredential(created.credential);
      toast({ status: "success", title: `${created.principal.name} created` });
      setPrincipalId("");
      setName("");
      setRoles(["tenant_member"]);
      setIssue(false);
      await onRefresh();
    } catch (exc) {
      toast({ status: "error", title: errorMessage(exc) });
    } finally {
      setBusy(false);
    }
  }

  return (
    <Grid templateColumns={{ base: "1fr", xl: "minmax(0, 1fr) 400px" }} gap={5}>
      <GridItem>
        <Panel title="Principals" icon={<Users size={18} />}>
          <TableView
            headers={["Principal", "Name", "Kind", "State"]}
            empty="No Principals"
            rows={data.principals.map((principal) => [
              <Code key="principal">{principal.principal_id}</Code>,
              principal.name,
              principal.kind,
              <Badge key="state" colorScheme={principal.active ? "green" : "gray"}>
                {principal.active ? "active" : "inactive"}
              </Badge>
            ])}
          />
          <Divider my={5} />
          <TableView
            headers={["Tenant", "Principal", "Roles"]}
            empty="No Memberships"
            rows={data.memberships.map((membership) => [
              <Code key="tenant">{membership.tenant_id}</Code>,
              <Code key="principal">{membership.principal_id}</Code>,
              <RoleBadges key="roles" roles={membership.roles} />
            ])}
          />
        </Panel>
      </GridItem>
      <GridItem>
        <Panel title="New User" icon={<Plus size={18} />}>
          <Stack as="form" spacing={4} onSubmit={submit}>
            <FormControl isRequired>
              <FormLabel>Tenant</FormLabel>
              <Select value={tenantId} onChange={(event) => setTenantId(event.target.value)}>
                {data.tenants.map((tenant) => (
                  <option key={tenant.tenant_id} value={tenant.tenant_id}>
                    {tenant.name}
                  </option>
                ))}
              </Select>
            </FormControl>
            <FormControl isRequired>
              <FormLabel>Principal ID</FormLabel>
              <Input value={principalId} onChange={(event) => setPrincipalId(event.target.value)} />
            </FormControl>
            <FormControl isRequired>
              <FormLabel>Name</FormLabel>
              <Input value={name} onChange={(event) => setName(event.target.value)} />
            </FormControl>
            <FormControl>
              <FormLabel>Tenant Roles</FormLabel>
              <CheckboxGroup value={roles} onChange={(next) => setRoles(next.map(String))}>
                <Stack>
                  {TENANT_ROLES.map((role) => (
                    <Checkbox key={role} value={role}>
                      {role}
                    </Checkbox>
                  ))}
                </Stack>
              </CheckboxGroup>
            </FormControl>
            <HStack justify="space-between">
              <FormLabel m={0}>Issue Token</FormLabel>
              <Switch isChecked={issue} onChange={(event) => setIssue(event.target.checked)} />
            </HStack>
            {issue ? (
              <FormControl>
                <FormLabel>Lifetime Days</FormLabel>
                <Input
                  type="number"
                  min={1}
                  max={90}
                  value={lifetime}
                  onChange={(event) => setLifetime(Number(event.target.value))}
                />
              </FormControl>
            ) : null}
            <Button type="submit" isLoading={busy} leftIcon={<Plus size={16} />}>
              Create User
            </Button>
          </Stack>
        </Panel>
      </GridItem>
    </Grid>
  );
}

function TokensPage({
  csrf,
  data,
  onCredential,
  onRefresh
}: {
  csrf: string | null;
  data: AdminData;
  onCredential: (credential: Credential | RotatedCredential | null) => void;
  onRefresh: () => Promise<void>;
}) {
  const toast = useToast();
  const [tenantId, setTenantId] = useState(data.tenants[0]?.tenant_id ?? "");
  const [principalId, setPrincipalId] = useState(data.principals[0]?.principal_id ?? "");
  const [tokens, setTokens] = useState<TokenRecord[]>([]);
  const [busy, setBusy] = useState(false);
  const [lifetime, setLifetime] = useState(30);
  const selectedPrincipal = data.principals.find((principal) => principal.principal_id === principalId);

  async function refreshTokens() {
    if (!tenantId || !principalId) {
      setTokens([]);
      return;
    }
    setTokens(await listTokens(tenantId, principalId));
  }

  useEffect(() => {
    void refreshTokens().catch((exc) => toast({ status: "error", title: errorMessage(exc) }));
  }, [tenantId, principalId]);

  async function issue() {
    setBusy(true);
    try {
      const credential = await issueToken(csrf, {
        tenant_id: tenantId,
        principal_id: principalId,
        lifetime_days: lifetime
      });
      onCredential(credential);
      await refreshTokens();
      await onRefresh();
    } catch (exc) {
      toast({ status: "error", title: errorMessage(exc) });
    } finally {
      setBusy(false);
    }
  }

  async function rotate(record: TokenRecord) {
    setBusy(true);
    try {
      const rotated = await rotateToken(csrf, record.token_id, 10, lifetime);
      onCredential(rotated);
      await refreshTokens();
      await onRefresh();
    } catch (exc) {
      toast({ status: "error", title: errorMessage(exc) });
    } finally {
      setBusy(false);
    }
  }

  async function revoke(record: TokenRecord) {
    setBusy(true);
    try {
      await revokeToken(csrf, record.token_id);
      await refreshTokens();
      await onRefresh();
    } catch (exc) {
      toast({ status: "error", title: errorMessage(exc) });
    } finally {
      setBusy(false);
    }
  }

  return (
    <VStack align="stretch" spacing={5}>
      <Panel title="Principal Tokens" icon={<KeyRound size={18} />}>
        <SimpleGrid columns={{ base: 1, md: 4 }} spacing={4} mb={5}>
          <FormControl>
            <FormLabel>Tenant</FormLabel>
            <Select value={tenantId} onChange={(event) => setTenantId(event.target.value)}>
              {data.tenants.map((tenant) => (
                <option key={tenant.tenant_id} value={tenant.tenant_id}>
                  {tenant.name}
                </option>
              ))}
            </Select>
          </FormControl>
          <FormControl>
            <FormLabel>Principal</FormLabel>
            <Select value={principalId} onChange={(event) => setPrincipalId(event.target.value)}>
              {data.principals.map((principal) => (
                <option key={principal.principal_id} value={principal.principal_id}>
                  {principal.name}
                </option>
              ))}
            </Select>
          </FormControl>
          <FormControl>
            <FormLabel>Lifetime Days</FormLabel>
            <Input
              type="number"
              min={1}
              max={90}
              value={lifetime}
              onChange={(event) => setLifetime(Number(event.target.value))}
            />
          </FormControl>
          <Flex align="end">
            <Button isLoading={busy} leftIcon={<Plus size={16} />} onClick={issue} w="full">
              Issue Token
            </Button>
          </Flex>
        </SimpleGrid>
        {selectedPrincipal ? (
          <Text fontSize="sm" color="ink.500" mb={4}>
            {selectedPrincipal.kind} / {selectedPrincipal.principal_id}
          </Text>
        ) : null}
        <TableView
          headers={["Token", "Roles", "Issued", "Expires", "State", ""]}
          empty="No Tokens"
          rows={tokens.map((token) => [
            <Code key="token">{shortId(token.token_id)}</Code>,
            <RoleBadges key="roles" roles={token.roles} />,
            formatDate(token.issued_at),
            formatDate(token.expires_at),
            <Badge key="state" colorScheme={token.active ? "green" : "gray"}>
              {token.active ? "active" : "inactive"}
            </Badge>,
            <HStack key="actions" justify="end">
              <Tooltip label="Rotate">
                <IconButton
                  aria-label="Rotate"
                  icon={<RotateCcw size={16} />}
                  size="sm"
                  variant="outline"
                  isDisabled={!token.active || busy}
                  onClick={() => rotate(token)}
                />
              </Tooltip>
              <Tooltip label="Revoke">
                <IconButton
                  aria-label="Revoke"
                  icon={<Ban size={16} />}
                  size="sm"
                  colorScheme="red"
                  variant="outline"
                  isDisabled={!token.active || busy}
                  onClick={() => revoke(token)}
                />
              </Tooltip>
            </HStack>
          ])}
        />
      </Panel>
    </VStack>
  );
}

function OperatorsPage({
  csrf,
  data,
  onCredential,
  onRefresh
}: {
  csrf: string | null;
  data: AdminData;
  onCredential: (credential: Credential | RotatedCredential | null) => void;
  onRefresh: () => Promise<void>;
}) {
  const toast = useToast();
  const [operatorId, setOperatorId] = useState(data.operators[0]?.operator_id ?? "");
  const [tokens, setTokens] = useState<OperatorTokenRecord[]>([]);
  const [newOperatorId, setNewOperatorId] = useState("");
  const [newOperatorName, setNewOperatorName] = useState("");
  const [roles, setRoles] = useState<string[]>(["tenant_support"]);
  const [lifetime, setLifetime] = useState(7);
  const [busy, setBusy] = useState(false);

  const selected = data.operators.find((operator) => operator.operator_id === operatorId);

  async function refreshTokens() {
    if (!operatorId) {
      setTokens([]);
      return;
    }
    setTokens(await listOperatorTokens(operatorId));
  }

  useEffect(() => {
    void refreshTokens().catch((exc) => toast({ status: "error", title: errorMessage(exc) }));
  }, [operatorId]);

  async function create(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    try {
      const created = await createOperator(csrf, {
        operator_id: newOperatorId,
        name: newOperatorName,
        roles
      });
      setOperatorId(created.operator_id);
      setNewOperatorId("");
      setNewOperatorName("");
      setRoles(["tenant_support"]);
      await onRefresh();
    } catch (exc) {
      toast({ status: "error", title: errorMessage(exc) });
    } finally {
      setBusy(false);
    }
  }

  async function issue() {
    if (!operatorId) {
      return;
    }
    setBusy(true);
    try {
      const credential = await issueOperatorToken(csrf, operatorId, lifetime);
      onCredential(credential);
      await refreshTokens();
      await onRefresh();
    } catch (exc) {
      toast({ status: "error", title: errorMessage(exc) });
    } finally {
      setBusy(false);
    }
  }

  async function rotate(record: OperatorTokenRecord) {
    setBusy(true);
    try {
      const rotated = await rotateOperatorToken(csrf, record.token_id, 10, lifetime);
      onCredential(rotated);
      await refreshTokens();
      await onRefresh();
    } catch (exc) {
      toast({ status: "error", title: errorMessage(exc) });
    } finally {
      setBusy(false);
    }
  }

  async function revoke(record: OperatorTokenRecord) {
    setBusy(true);
    try {
      await revokeOperatorToken(csrf, record.token_id);
      await refreshTokens();
      await onRefresh();
    } catch (exc) {
      toast({ status: "error", title: errorMessage(exc) });
    } finally {
      setBusy(false);
    }
  }

  async function disable(operator: Operator) {
    setBusy(true);
    try {
      await updateOperator(csrf, operator.operator_id, { active: false });
      await onRefresh();
    } catch (exc) {
      toast({ status: "error", title: errorMessage(exc) });
    } finally {
      setBusy(false);
    }
  }

  return (
    <Grid templateColumns={{ base: "1fr", xl: "minmax(0, 1fr) 430px" }} gap={5}>
      <GridItem>
        <Panel title="Operators" icon={<ShieldCheck size={18} />}>
          <TableView
            headers={["Operator", "Name", "Roles", "State", ""]}
            empty="No Operators"
            rows={data.operators.map((operator) => [
              <Code key="operator">{operator.operator_id}</Code>,
              operator.name,
              <RoleBadges key="roles" roles={operator.roles} />,
              <Badge key="state" colorScheme={operator.active ? "green" : "gray"}>
                {operator.active ? "active" : "inactive"}
              </Badge>,
              <HStack key="actions" justify="end">
                <Button size="sm" variant="outline" onClick={() => setOperatorId(operator.operator_id)}>
                  Tokens
                </Button>
                <Tooltip label="Disable">
                  <IconButton
                    aria-label="Disable"
                    icon={<Ban size={16} />}
                    size="sm"
                    colorScheme="red"
                    variant="outline"
                    isDisabled={!operator.active || busy}
                    onClick={() => disable(operator)}
                  />
                </Tooltip>
              </HStack>
            ])}
          />
        </Panel>
      </GridItem>
      <GridItem>
        <VStack align="stretch" spacing={5}>
          <Panel title="New Operator" icon={<Plus size={18} />}>
            <Stack as="form" spacing={4} onSubmit={create}>
              <FormControl isRequired>
                <FormLabel>Operator ID</FormLabel>
                <Input value={newOperatorId} onChange={(event) => setNewOperatorId(event.target.value)} />
              </FormControl>
              <FormControl isRequired>
                <FormLabel>Name</FormLabel>
                <Input value={newOperatorName} onChange={(event) => setNewOperatorName(event.target.value)} />
              </FormControl>
              <FormControl>
                <FormLabel>Roles</FormLabel>
                <CheckboxGroup value={roles} onChange={(next) => setRoles(next.map(String))}>
                  <Stack>
                    {OPERATOR_ROLES.map((role) => (
                      <Checkbox key={role} value={role}>
                        {role}
                      </Checkbox>
                    ))}
                  </Stack>
                </CheckboxGroup>
              </FormControl>
              <Button type="submit" isLoading={busy} leftIcon={<Plus size={16} />}>
                Create Operator
              </Button>
            </Stack>
          </Panel>
          <Panel title="Operator Tokens" icon={<KeyRound size={18} />}>
            <Stack spacing={4}>
              <FormControl>
                <FormLabel>Operator</FormLabel>
                <Select value={operatorId} onChange={(event) => setOperatorId(event.target.value)}>
                  {data.operators.map((operator) => (
                    <option key={operator.operator_id} value={operator.operator_id}>
                      {operator.name}
                    </option>
                  ))}
                </Select>
              </FormControl>
              {selected ? <RoleBadges roles={selected.roles} /> : null}
              <HStack align="end">
                <FormControl>
                  <FormLabel>Lifetime Days</FormLabel>
                  <Input
                    type="number"
                    min={1}
                    max={30}
                    value={lifetime}
                    onChange={(event) => setLifetime(Number(event.target.value))}
                  />
                </FormControl>
                <Button isLoading={busy} onClick={issue} leftIcon={<Plus size={16} />}>
                  Issue
                </Button>
              </HStack>
              <TableView
                headers={["Token", "State", "Expires", ""]}
                empty="No Operator Tokens"
                rows={tokens.map((token) => [
                  <Code key="token">{shortId(token.token_id)}</Code>,
                  <Badge key="state" colorScheme={token.active ? "green" : "gray"}>
                    {token.active ? "active" : "inactive"}
                  </Badge>,
                  formatDate(token.expires_at),
                  <HStack key="actions" justify="end">
                    <Tooltip label="Rotate">
                      <IconButton
                        aria-label="Rotate"
                        icon={<RotateCcw size={16} />}
                        size="sm"
                        variant="outline"
                        isDisabled={!token.active || busy}
                        onClick={() => rotate(token)}
                      />
                    </Tooltip>
                    <Tooltip label="Revoke">
                      <IconButton
                        aria-label="Revoke"
                        icon={<Ban size={16} />}
                        size="sm"
                        variant="outline"
                        colorScheme="red"
                        isDisabled={!token.active || busy}
                        onClick={() => revoke(token)}
                      />
                    </Tooltip>
                  </HStack>
                ])}
              />
            </Stack>
          </Panel>
        </VStack>
      </GridItem>
    </Grid>
  );
}

function ProvisioningPage({
  csrf,
  data,
  onRefresh
}: {
  csrf: string | null;
  data: AdminData;
  onRefresh: () => Promise<void>;
}) {
  const toast = useToast();
  const [busyJob, setBusyJob] = useState<string | null>(null);

  async function cancel(job: ProvisioningJob) {
    setBusyJob(job.job_id);
    try {
      await cancelProvisioningJob(csrf, job.job_id);
      await onRefresh();
    } catch (exc) {
      toast({ status: "error", title: errorMessage(exc) });
    } finally {
      setBusyJob(null);
    }
  }

  return (
    <Panel title="Provisioning Jobs" icon={<ClipboardList size={18} />}>
      <TableView
        headers={["Job", "Tenant", "State", "Attempt", "Updated", ""]}
        empty="No Provisioning Jobs"
        rows={data.provisioningJobs.map((job) => [
          <Code key="job">{shortId(job.job_id)}</Code>,
          <Code key="tenant">{job.tenant_id}</Code>,
          <StatusBadge key="state" value={job.state} />,
          job.attempt,
          formatDate(job.updated_at),
          <HStack key="actions" justify="end">
            <Tooltip label="Cancel">
              <IconButton
                aria-label="Cancel"
                icon={<Ban size={16} />}
                size="sm"
                colorScheme="red"
                variant="outline"
                isLoading={busyJob === job.job_id}
                isDisabled={!["queued", "running"].includes(job.state)}
                onClick={() => cancel(job)}
              />
            </Tooltip>
          </HStack>
        ])}
      />
    </Panel>
  );
}

function AuditPage({ data }: { data: AdminData }) {
  return (
    <Panel title="Operator Audit Events" icon={<ScrollText size={18} />}>
      <TableView
        headers={["Action", "Actor", "Target", "Outcome", "When"]}
        empty="No Operator Audit Events"
        rows={data.auditEvents.map((event) => [
          event.action,
          <Code key="actor">{event.operator_id}</Code>,
          <Code key="target">{targetLabel(event.target_ids)}</Code>,
          <Badge key="outcome" colorScheme={event.outcome === "succeeded" ? "green" : "red"}>
            {event.outcome}
          </Badge>,
          formatDate(event.created_at)
        ])}
      />
    </Panel>
  );
}

function Panel({ title, icon, children }: { title: string; icon: ReactNode; children: ReactNode }) {
  return (
    <Box bg="white" border="1px solid" borderColor="ink.100" borderRadius="8px" p={5}>
      <HStack mb={5} spacing={3}>
        <Flex align="center" justify="center" boxSize="34px" borderRadius="8px" bg="brand.50" color="brand.700">
          {icon}
        </Flex>
        <Heading size="sm">{title}</Heading>
      </HStack>
      {children}
    </Box>
  );
}

function StatCard({ label, value, helper }: { label: string; value: number; helper: string }) {
  return (
    <Stat bg="white" border="1px solid" borderColor="ink.100" borderRadius="8px" p={5}>
      <StatLabel color="ink.500">{label}</StatLabel>
      <StatNumber fontSize="3xl">{value}</StatNumber>
      <StatHelpText mb={0}>{helper}</StatHelpText>
    </Stat>
  );
}

function TableView({
  headers,
  rows,
  empty
}: {
  headers: string[];
  rows: Array<Array<ReactNode>>;
  empty: string;
}) {
  if (rows.length === 0) {
    return (
      <Flex align="center" justify="center" minH="120px" border="1px dashed" borderColor="ink.200" borderRadius="8px">
        <Text color="ink.500">{empty}</Text>
      </Flex>
    );
  }

  return (
    <TableContainer>
      <Table size="sm">
        <Thead>
          <Tr>
            {headers.map((header) => (
              <Th key={header}>{header}</Th>
            ))}
          </Tr>
        </Thead>
        <Tbody>
          {rows.map((cells, rowIndex) => (
            <Tr key={rowIndex}>
              {cells.map((cell, cellIndex) => (
                <Td key={`${rowIndex}-${cellIndex}`}>{cell}</Td>
              ))}
            </Tr>
          ))}
        </Tbody>
      </Table>
    </TableContainer>
  );
}

function RoleBadges({ roles }: { roles: string[] }) {
  return (
    <HStack wrap="wrap" spacing={1}>
      {roles.map((role) => (
        <Badge key={role} colorScheme="brand" variant="subtle">
          {role}
        </Badge>
      ))}
    </HStack>
  );
}

function StatusBadge({ value }: { value: string }) {
  const color = useMemo(() => {
    if (value === "succeeded" || value === "cleaned_up") {
      return "green";
    }
    if (value === "failed") {
      return "red";
    }
    if (value === "running") {
      return "blue";
    }
    if (value.includes("cancel") || value.includes("cleanup")) {
      return "orange";
    }
    return "gray";
  }, [value]);
  return <Badge colorScheme={color}>{value}</Badge>;
}

function CredentialBanner({
  credential,
  onCopy,
  onClose
}: {
  credential: Credential | RotatedCredential | null;
  onCopy: (value: string) => Promise<void>;
  onClose: () => void;
}) {
  if (!credential) {
    return null;
  }
  const shown = "credential" in credential ? credential.credential : credential;

  return (
    <Alert status="success" borderRadius="8px" mb={5} alignItems="flex-start">
      <AlertIcon mt={1} />
      <Box flex="1" minW={0}>
        <HStack mb={2}>
          <Text fontWeight={700}>One-time token</Text>
          {"previous_token_id" in credential ? (
            <Badge colorScheme="orange">rotated {shortId(credential.previous_token_id)}</Badge>
          ) : null}
        </HStack>
        <Code display="block" whiteSpace="normal" wordBreak="break-all" p={3}>
          {shown.access_token}
        </Code>
        <Text mt={2} fontSize="sm">
          Expires {formatDate(shown.expires_at)}
        </Text>
      </Box>
      <HStack>
        <Tooltip label="Copy">
          <IconButton aria-label="Copy" icon={<Copy size={16} />} onClick={() => onCopy(shown.access_token)} />
        </Tooltip>
        <Button variant="outline" colorScheme="gray" onClick={onClose}>
          Dismiss
        </Button>
      </HStack>
    </Alert>
  );
}

function formatDate(value: string | null) {
  if (!value) {
    return "n/a";
  }
  return new Intl.DateTimeFormat(undefined, {
    month: "short",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit"
  }).format(new Date(value));
}

function shortId(value: string) {
  return value.length <= 12 ? value : `${value.slice(0, 8)}...`;
}

function activeCount(items: Array<{ active: boolean }>) {
  return items.filter((item) => item.active).length;
}

function idempotencyKey(prefix: string) {
  return `${prefix}-${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

function targetLabel(targetIds: Record<string, string>) {
  const first = Object.entries(targetIds)[0];
  if (!first) {
    return "n/a";
  }
  return `${first[0]}=${shortId(first[1])}`;
}

function errorMessage(exc: unknown) {
  if (exc instanceof ApiError) {
    return exc.message;
  }
  if (exc instanceof Error) {
    return exc.message;
  }
  return "Request failed";
}
