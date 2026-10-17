"""Optional local semantic SEC retrieval fused with existing SQLite FTS5.

Retrieval scores are ranking signals, never financial metrics or confidence.
No network, model API, ANN service, history rewrite or automatic model download.
"""
import hashlib
import math
from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from typing import Protocol

from .domain import utc
from .provider import ProviderError, SYMBOL
from .tdb import content_hash

MODEL_SHA = '98a01d88b7de996cdea58c32ca71208c09968d143798814b2ea09d3439dc334f'
TOKENIZER_SHA = '2c3387be76557bd40970cec13153b3bbf80407865484b209e655e5e4729076b8'


class EmbeddingError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


class Embedder(Protocol):
    dimension: int
    fingerprint: str
    def encode(self, texts: list[str]) -> list[tuple[float, ...]]: ...


def normalized(vector, dimension):
    if len(vector) != dimension or any(isinstance(v,bool) or not isinstance(v,(int,float)) or not math.isfinite(v) for v in vector):
        raise EmbeddingError('invalid_vector')
    norm = math.sqrt(sum(v*v for v in vector))
    if not math.isfinite(norm) or norm <= 0:
        raise EmbeddingError('invalid_vector')
    return tuple(float(v/norm) for v in vector)


class LocalONNXEmbedder:
    dimension = 384
    def __init__(self, folder):
        try:
            import numpy as np
            import onnxruntime as ort
            from tokenizers import Tokenizer
        except ImportError:
            raise EmbeddingError('embedding_dependencies_missing') from None
        folder = Path(folder)
        model, tokenizer = folder/'model_quint8_avx2.onnx', folder/'tokenizer.json'
        try:
            for path,digest in ((model,MODEL_SHA),(tokenizer,TOKENIZER_SHA)):
                if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                    raise EmbeddingError('embedding_artifact_integrity')
            self.tokenizer = Tokenizer.from_file(str(tokenizer))
            self.tokenizer.enable_truncation(max_length=128)
            self.tokenizer.enable_padding()
            options = ort.SessionOptions();options.intra_op_num_threads=2;options.inter_op_num_threads=1
            self.session = ort.InferenceSession(str(model),sess_options=options,providers=['CPUExecutionProvider'])
        except OSError:
            raise EmbeddingError('embedding_artifact_missing') from None
        except EmbeddingError:
            raise
        except Exception:
            raise EmbeddingError('embedding_runtime_unavailable') from None
        self.np = np
        self.fingerprint = content_hash({'model_sha256':MODEL_SHA,'tokenizer_sha256':TOKENIZER_SHA,
            'pooling':'masked_mean_l2','tokens':128,'dimension':384})
        self._lock = RLock()

    def encode(self, texts):
        vectors = []
        with self._lock:
            for start in range(0,len(texts),16):
                batch = self.tokenizer.encode_batch(texts[start:start+16])
                arrays = {'input_ids':self.np.array([e.ids for e in batch],dtype=self.np.int64),
                    'attention_mask':self.np.array([e.attention_mask for e in batch],dtype=self.np.int64),
                    'token_type_ids':self.np.array([e.type_ids for e in batch],dtype=self.np.int64)}
                try:
                    output = self.session.run(None,{i.name:arrays[i.name] for i in self.session.get_inputs()})[0]
                    mask = arrays['attention_mask'][...,None]
                    pooled = (output*mask).sum(axis=1)/self.np.maximum(mask.sum(axis=1),1)
                    vectors.extend(normalized(row.tolist(),self.dimension) for row in pooled)
                except EmbeddingError:
                    raise
                except Exception:
                    raise EmbeddingError('embedding_inference_failed') from None
        return vectors


class ExactCosineIndex:
    """Bounded in-memory index with model/content identity and stable exact ranks."""
    def __init__(self, embedder, *, maximum=1024):
        self.embedder, self.maximum = embedder, maximum
        self._vectors = {}

    def rank(self, chunks, query):
        if len(chunks) > self.maximum:
            raise EmbeddingError('vector_candidate_budget')
        keys = [(c.chunk_id,c.content_hash,self.embedder.fingerprint) for c in chunks]
        missing = [i for i,key in enumerate(keys) if key not in self._vectors]
        encoded = self.embedder.encode([chunks[i].section+'. '+chunks[i].text for i in missing]) if missing else []
        if len(encoded) != len(missing):
            raise EmbeddingError('embedding_count_mismatch')
        if len(self._vectors)+len(missing) > self.maximum*2:
            self._vectors.clear()
            missing = list(range(len(chunks)))
            encoded = self.embedder.encode([c.section+'. '+c.text for c in chunks])
            if len(encoded) != len(missing):
                raise EmbeddingError('embedding_count_mismatch')
        for i,vector in zip(missing,encoded,strict=True):
            self._vectors[keys[i]] = normalized(vector,self.embedder.dimension)
        query_vectors = self.embedder.encode([query])
        if len(query_vectors) != 1:
            raise EmbeddingError('embedding_count_mismatch')
        question = normalized(query_vectors[0],self.embedder.dimension)
        ranked = [(sum(a*b for a,b in zip(question,normalized(self._vectors[key],self.embedder.dimension),strict=True)),chunk)
                  for key,chunk in zip(keys,chunks,strict=True)]
        return [chunk for _,chunk in sorted(ranked,key=lambda pair:(-pair[0],pair[1].chunk_id))]


def reciprocal_rank_fusion(keyword, vector, limit):
    scores, chunks = {}, {}
    for rows in (keyword,vector):
        seen = set()
        for rank,chunk in enumerate(rows,1):
            if chunk.chunk_id in seen:
                continue
            seen.add(chunk.chunk_id);chunks[chunk.chunk_id]=chunk
            scores[chunk.chunk_id]=scores.get(chunk.chunk_id,0)+1/(60+rank)
    return [chunks[key] for key in sorted(scores,key=lambda key:(-scores[key],key))[:limit]]


@dataclass(frozen=True)
class RetrievalResult:
    chunks: list
    effective_mode: str
    fallback_reason: str | None
    keyword_ids: list[str]
    vector_ids: list[str]
    model_fingerprint: str | None


class HybridSECRetriever:
    def __init__(self, store, embedder=None, *, model_folder=None, maximum_candidates=1024):
        self.store, self.embedder, self.model_folder = store, embedder, model_folder
        self.maximum = maximum_candidates
        self.index = ExactCosineIndex(embedder,maximum=maximum_candidates) if embedder else None
        self._lock = RLock()

    @staticmethod
    def validate_source(chunk,symbol,cutoff,forms):
        from .sec import _hash
        original={key:getattr(chunk,key) for key in ('document_id','sequence','section','text')}
        if (chunk.symbol!=symbol or chunk.form not in forms or chunk.filed_at>cutoff or _hash(original)!=chunk.content_hash):
            # Source corruption cannot be downgraded to a vector availability issue.
            raise ProviderError('invalid_data','SEC source identity/content integrity failed')

    def search(self, symbol, query, cutoff, forms, limit=5):
        cutoff = utc(cutoff)
        if not SYMBOL.fullmatch(symbol) or not query.strip() or not forms or not 1<=limit<=20:
            raise ProviderError('invalid_input','Invalid hybrid SEC request')
        with self._lock:
            # Independent lists, not a semantic reranker of FTS5's subset.
            keyword = self.store.search_sec_chunks(symbol,query,cutoff,forms,min(limit*4,80))
            for chunk in keyword:self.validate_source(chunk,symbol,cutoff,forms)
            try:
                if self.index is None:
                    if not self.model_folder:
                        raise EmbeddingError('embedding_not_configured')
                    self.embedder = LocalONNXEmbedder(self.model_folder)
                    self.index = ExactCosineIndex(self.embedder,maximum=self.maximum)
                candidates = self.store.eligible_sec_chunks(symbol,cutoff,forms,self.maximum+1)
                # Revalidate loaded metadata and content hashes before vector use.
                for chunk in candidates:self.validate_source(chunk,symbol,cutoff,forms)
                vector = self.index.rank(candidates,query)[:min(limit*4,80)]
                fused = reciprocal_rank_fusion(keyword,vector,limit)
                return RetrievalResult(fused,'HYBRID',None,[c.chunk_id for c in keyword],
                    [c.chunk_id for c in vector],self.embedder.fingerprint)
            except EmbeddingError as exc:
                return RetrievalResult(keyword[:limit],'FTS5',exc.code,[c.chunk_id for c in keyword],[],None)
