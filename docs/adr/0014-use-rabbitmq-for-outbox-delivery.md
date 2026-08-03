# Use RabbitMQ for outbox delivery

Iteration 1 uses RabbitMQ durable queues, acknowledgements, retries, and dead-letter queues to deliver committed outbox events to memory workers. PostgreSQL remains authoritative so broker outage or redelivery cannot lose or duplicate a command, and Redis is omitted until a distinct cache or coordination need appears.
