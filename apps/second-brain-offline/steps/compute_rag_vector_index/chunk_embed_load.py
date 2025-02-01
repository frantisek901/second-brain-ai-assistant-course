from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Generator

from langchain_core.documents import Document as LangChainDocument
from langchain_text_splitters import RecursiveCharacterTextSplitter
from loguru import logger
from tqdm import tqdm
from zenml.steps import step

from second_brain_offline.application.rag import (
    EmbeddingModelType,
    get_retriever,
    get_splitter,
)
from second_brain_offline.domain import Document
from second_brain_offline.infrastructure.mongo import MongoDBHybridIndex, MongoDBService


@step
def chunk_embed_load(
    documents: list[Document],
    collection_name: str,
    processing_batch_size: int,
    processing_max_workers: int,
    embedding_model_id: str,
    embedding_model_type: EmbeddingModelType,
    embedding_model_dim: int,
    chunk_size: int,
    contextual_agent_model_id: str,
    contextual_agent_max_characters: int,
    mock: bool = False,
    device: str = "cpu",
) -> None:
    """Process documents by chunking, embedding, and loading into MongoDB.

    Args:
        documents: List of documents to process.
        collection_name: Name of MongoDB collection to store documents.
        processing_batch_size: Number of documents to process in each batch.
        processing_max_workers: Maximum number of concurrent processing threads.
        embedding_model_id: Identifier for the embedding model.
        embedding_model_type: Type of embedding model to use.
        embedding_model_dim: Dimension of the embedding vectors.
        chunk_size: Size of text chunks for splitting documents.
        device: Device to run embeddings on ('cpu' or 'cuda'). Defaults to 'cpu'.
    """

    retriever = get_retriever(
        embedding_model_id=embedding_model_id,
        embedding_model_type=embedding_model_type,
        device=device,
    )
    splitter = get_splitter(
        chunk_size=chunk_size,
        model_id=contextual_agent_model_id,
        max_characters=contextual_agent_max_characters,
        mock=mock,
        max_concurrent_requests=processing_max_workers,
    )

    with MongoDBService(
        model=Document, collection_name=collection_name
    ) as mongodb_client:
        mongodb_client.clear_collection()

        docs = [
            LangChainDocument(
                page_content=doc.content, metadata=doc.metadata.model_dump()
            )
            for doc in documents
            if doc
        ]
        process_docs(
            retriever,
            docs,
            splitter=splitter,
            batch_size=processing_batch_size,
            max_workers=processing_max_workers,
        )

        hybrid_index = MongoDBHybridIndex(
            retriever=retriever,
            mongodb_client=mongodb_client,
        )
        hybrid_index.create(embedding_dim=embedding_model_dim)


def process_docs(
    retriever: Any,
    docs: list[LangChainDocument],
    splitter: RecursiveCharacterTextSplitter,
    batch_size: int = 4,
    max_workers: int = 2,
) -> list[None]:
    """Process LangChain documents into MongoDB using thread pool.

    Args:
        retriever: MongoDB Atlas document retriever instance.
        docs: List of LangChain documents to process.
        splitter: Text splitter instance for chunking documents.
        batch_size: Number of documents to process in each batch. Defaults to 256.
        max_workers: Maximum number of concurrent threads. Defaults to 2.

    Returns:
        List of None values representing completed batch processing results.
    """
    batches = list(get_batches(docs, batch_size))
    results = []
    total_docs = len(docs)

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [
            executor.submit(process_batch, retriever, batch, splitter)
            for batch in batches
        ]

        with tqdm(total=total_docs, desc="Processing documents") as pbar:
            for future in as_completed(futures):
                result = future.result()
                results.append(result)
                pbar.update(batch_size)

    return results


def get_batches(
    docs: list[LangChainDocument], batch_size: int
) -> Generator[list[LangChainDocument], None, None]:
    """Return batches of documents to ingest into MongoDB.

    Args:
        docs: List of LangChain documents to batch.
        batch_size: Number of documents in each batch.

    Yields:
        Batches of documents of size batch_size.
    """
    for i in range(0, len(docs), batch_size):
        yield docs[i : i + batch_size]


def process_batch(
    retriever: Any,
    batch: list[LangChainDocument],
    splitter: RecursiveCharacterTextSplitter,
) -> None:
    """Ingest batches of documents into MongoDB by splitting and embedding.

    Args:
        retriever: MongoDB Atlas document retriever instance.
        batch: List of documents to ingest in this batch.
        splitter: Text splitter instance for chunking documents.

    Raises:
        Exception: If there is an error processing the batch of documents.
    """
    try:
        split_docs = splitter.split_documents(batch)
        retriever.vectorstore.add_documents(split_docs)

        logger.info(
            f"Successfully processed {len(batch)} documents into {len(split_docs)} chunks"
        )
    except Exception as e:
        logger.warning(f"Error processing batch of {len(batch)} documents: {str(e)}")
