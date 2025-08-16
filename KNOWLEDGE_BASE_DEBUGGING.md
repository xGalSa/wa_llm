# Knowledge Base Debug Logging Guide

## Overview
The Knowledge Base implementation now includes comprehensive logging to help debug issues at every stage of processing.

## Log Levels

### INFO Level (Default Production Logs)
- Request start/end markers
- Processing steps summary
- Results and metrics
- Timing information

### DEBUG Level (Detailed Debug Logs)
- Keyword extraction details
- Search query details  
- Individual topic filtering decisions
- AI agent input/output previews
- Database query execution details

## Logging Categories

### 1. **Request Lifecycle**
```
INFO  === KNOWLEDGE BASE ANSWERS START ===
INFO  Knowledge base processing message from: sender_jid
INFO  Knowledge base message text: 'user query...'
INFO  Total processing time: 2.34 seconds
INFO  === KNOWLEDGE BASE ANSWERS END ===
```

### 2. **Health & Initialization**
```
INFO  KnowledgeBaseAnswers initialized with threshold=0.4, max_topics=15
DEBUG Logger level: 20, Debug enabled: False
INFO  Knowledge base health check: 1234 topics available
```

### 3. **Query Processing**
```
INFO  Using rephrased query: 'how to configure database settings'
DEBUG Starting rephrasing agent for message: 'איך מגדירים בסיס נתונים...'
DEBUG Rephrasing completed: 'how to configure database settings...'
```

### 4. **Search Process**
```
DEBUG Extracted keywords for search: ['database', 'configuration', 'settings']
DEBUG Filtering search to groups: ['group1@g.us', 'group2@g.us']
DEBUG Executing semantic search with threshold < 0.4
DEBUG Semantic search returned 8 topics
DEBUG Keyword search returned 3 topics
DEBUG Combined results: 8 semantic + 2 keyword-only = 10 total unique topics
DEBUG Final topic distances: min=0.156, max=0.387, avg=0.264
```

### 5. **Quality Filtering**
```
DEBUG Filtering 10 topics for quality
DEBUG Accepted topic 'Database Configuration Best Practices...' - distance: 0.156, confidence: 0.610
DEBUG Filtered topic 'Unrelated Topic...' - distance 0.450 >= threshold 0.4
DEBUG Quality filtering results: 7 passed, 3 filtered out, avg confidence: 0.723
```

### 6. **Response Generation**
```
INFO  Retrieved 10 total topics, 7 quality topics
INFO  Confidence score: 0.723
DEBUG Starting generation agent with 7 topics, confidence: 0.723
DEBUG Generation prompt length: 3456 chars, history messages: 12
DEBUG Generation completed successfully, response length: 342 chars
```

### 7. **Error Handling**
```
WARNING Failed to rephrase query, using original: API timeout
ERROR   Failed to generate embedding: Connection refused
ERROR   Failed to search knowledge base: Database connection lost
```

## Key Metrics Tracked

### Performance Metrics
- **Total processing time**: End-to-end request processing
- **Topic retrieval counts**: Semantic vs keyword search results
- **Confidence scores**: Quality assessment of retrieved topics
- **Response characteristics**: Length, token usage estimates

### Quality Metrics
- **Similarity distances**: How well topics match the query
- **Filtering decisions**: Why topics were accepted/rejected
- **Confidence thresholds**: When cautious responses are triggered

### Error Tracking
- **Fallback triggers**: When original query is used vs rephrased
- **API failures**: Embedding, search, or generation failures
- **Data quality issues**: Empty knowledge base, malformed topics

## Debug Configuration

### Enable Debug Logging
```python
import logging
logging.getLogger('src.handler.knowledge_base_answers').setLevel(logging.DEBUG)
```

### Key Debug Information
1. **Keywords extracted** from user queries
2. **Group filtering** scope and decisions  
3. **Database query execution** results
4. **Topic-by-topic filtering** decisions with reasons
5. **AI agent interactions** with input/output previews
6. **Performance bottlenecks** and timing breakdowns

## Troubleshooting Common Issues

### No Results Found
Look for:
- `Knowledge base health check: 0 topics available` 
- `No relevant topics found above similarity threshold`
- `Extracted keywords for search: []`

### Poor Quality Results  
Look for:
- `Low confidence score (0.234), sending cautious response`
- `Quality filtering results: 2 passed, 8 filtered out`
- `Final topic distances: min=0.389, max=0.398`

### Performance Issues
Look for:
- `Total processing time: 12.45 seconds` (should be < 5s)
- `Generation prompt length: 45000 chars` (should be < 15000)
- `Semantic search returned 150 topics` (should be < 20)

### API Failures
Look for:
- `Failed to generate embedding: [error details]`
- `Failed to search knowledge base: [error details]`  
- `Generation agent failed: [error details]`

## Best Practices

1. **Monitor confidence scores** - consistently low scores indicate threshold tuning needed
2. **Track filtering ratios** - high filter rates suggest quality issues in knowledge base
3. **Watch processing times** - spikes indicate performance bottlenecks
4. **Review keyword extraction** - poor keywords lead to poor retrieval
5. **Check error patterns** - repeated API failures indicate infrastructure issues

This comprehensive logging enables full visibility into the knowledge base system's operation and decision-making process.
