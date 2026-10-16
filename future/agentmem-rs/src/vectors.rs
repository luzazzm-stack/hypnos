//! Local meaning-based search (bge-small-en-v1.5, ONNX on the CPU). One model per process, shared by every memory.
//! Vectors are stored as normalised float32 blobs, the same format the Python engine writes, so both can read them.
use crate::store::Store;
use std::collections::HashSet;
use std::path::Path;
use std::sync::Arc;

pub const QPREFIX: &str = "Represent this sentence for searching relevant passages: ";

pub struct Vectors {
    pub enabled: bool,
    store: Arc<Store>,
}

#[cfg(feature = "vectors")]
mod model {
    use fastembed::{EmbeddingModel, InitOptions, TextEmbedding};
    use std::sync::{Mutex, OnceLock};
    static MODEL: OnceLock<Option<Mutex<TextEmbedding>>> = OnceLock::new();

    pub fn embed(cache: &std::path::Path, texts: Vec<String>) -> Option<Vec<Vec<f32>>> {
        let m = MODEL.get_or_init(|| {
            TextEmbedding::try_new(InitOptions::new(EmbeddingModel::BGESmallENV15).with_cache_dir(cache.to_path_buf()).with_show_download_progress(false))
                .ok().map(Mutex::new)
        });
        let m = m.as_ref()?;
        let out = m.lock().ok()?.embed(texts, Some(8)).ok()?;
        Some(out.into_iter().map(|v| {
            let n = v.iter().map(|x| x * x).sum::<f32>().sqrt().max(1e-12);
            v.into_iter().map(|x| x / n).collect()
        }).collect())
    }
}

#[cfg(not(feature = "vectors"))]
mod model {
    pub fn embed(_cache: &std::path::Path, _texts: Vec<String>) -> Option<Vec<Vec<f32>>> {
        None
    }
}

fn to_bytes(v: &[f32]) -> Vec<u8> {
    v.iter().flat_map(|x| x.to_le_bytes()).collect()
}

fn from_bytes(b: &[u8]) -> Vec<f32> {
    b.chunks_exact(4).map(|c| f32::from_le_bytes([c[0], c[1], c[2], c[3]])).collect()
}

fn dot(a: &[f32], b: &[f32]) -> f32 {
    a.iter().zip(b).map(|(x, y)| x * y).sum()
}

impl Vectors {
    pub fn new(enabled: bool, store: Arc<Store>) -> Vectors {
        Vectors { enabled: enabled && cfg!(feature = "vectors"), store }
    }

    fn cache_dir(&self) -> std::path::PathBuf {
        self.store.path.parent().and_then(Path::parent).map(|h| h.join("models")).unwrap_or_else(|| "models".into())
    }

    fn embed(&self, texts: Vec<String>, query: bool) -> Option<Vec<Vec<f32>>> {
        let texts = texts.into_iter().map(|t| if query { format!("{}{}", QPREFIX, t) } else { t.chars().take(1200).collect() }).collect();
        model::embed(&self.cache_dir(), texts)
    }

    /// items: (ref, text); only embeds refs not stored yet.
    pub fn index(&self, kind: &str, items: Vec<(String, String)>) {
        if !self.enabled || items.is_empty() {
            return;
        }
        let have = self.store.vector_refs(kind);
        let todo: Vec<(String, String)> = items.into_iter().filter(|(r, _)| !have.contains(r)).collect();
        for chunk in todo.chunks(32) {
            if let Some(vecs) = self.embed(chunk.iter().map(|(_, t)| t.clone()).collect(), false) {
                for ((r, _), v) in chunk.iter().zip(vecs) {
                    let _ = self.store.put_vector(kind, r, &to_bytes(&v));
                }
            }
        }
    }

    pub fn search(&self, kind: &str, query: &str, k: usize) -> Vec<String> {
        if !self.enabled {
            return vec![];
        }
        let rows = self.store.vectors(kind);
        if rows.is_empty() {
            return vec![];
        }
        let q = match self.embed(vec![query.to_string()], true) { Some(mut v) => v.remove(0), None => return vec![] };
        let mut scored: Vec<(f32, String)> = rows.into_iter().map(|(r, b)| (dot(&from_bytes(&b), &q), r)).collect();
        scored.sort_by(|a, b| b.0.partial_cmp(&a.0).unwrap_or(std::cmp::Ordering::Equal));
        scored.into_iter().take(k).map(|(_, r)| r).collect()
    }

    /// Order the given refs by meaning-similarity to the query (refs without a vector go last).
    pub fn rank(&self, kind: &str, query: &str, refs: &[String]) -> Vec<String> {
        if !self.enabled || refs.is_empty() {
            return refs.to_vec();
        }
        let want: HashSet<&String> = refs.iter().collect();
        let rows: Vec<(String, Vec<u8>)> = self.store.vectors(kind).into_iter().filter(|(r, _)| want.contains(r)).collect();
        let q = match self.embed(vec![query.to_string()], true) { Some(mut v) => v.remove(0), None => return refs.to_vec() };
        let mut scored: Vec<(f32, String)> = rows.into_iter().map(|(r, b)| (dot(&from_bytes(&b), &q), r)).collect();
        scored.sort_by(|a, b| b.0.partial_cmp(&a.0).unwrap_or(std::cmp::Ordering::Equal));
        let mut out: Vec<String> = scored.into_iter().map(|(_, r)| r).collect();
        let seen: HashSet<String> = out.iter().cloned().collect();
        out.extend(refs.iter().filter(|r| !seen.contains(*r)).cloned());
        out
    }
}
