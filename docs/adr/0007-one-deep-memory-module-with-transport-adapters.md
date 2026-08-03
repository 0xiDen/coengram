# Expose one deep memory module through transport adapters

Authorization, scoping, recall, retention, Promotion, provenance, and audit semantics live behind one deep memory module interface rather than mirroring Neo4j SDK methods. MCP and typed HTTP are adapters at that seam: Claude and LangChain use MCP where discovery helps, ActiveGraph uses typed operations where deterministic contracts matter, and both receive identical behavior without direct Bolt or PostgreSQL access.
