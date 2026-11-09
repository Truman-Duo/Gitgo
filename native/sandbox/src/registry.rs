// Tokens are opaque, scoped to the loaded module and tagged by resource type.
// Each issued ordinal corresponds to a successful insertion. Therefore an
// issued token missing from the live map is an exact retirement receipt.
use std::collections::BTreeMap;

pub(crate) struct Registry<T, const TAG: u64> {
    issued: u64,
    pub(crate) live: BTreeMap<u64, T>,
}

impl<T, const TAG: u64> Registry<T, TAG> {
    pub(crate) fn new() -> Self {
        Self {
            issued: 0,
            live: BTreeMap::new(),
        }
    }

    // Check exhaustion before an OS call, but issue nothing if the call fails.
    pub(crate) fn next(&self) -> Option<u64> {
        (self.issued < (u64::MAX >> 2)).then(|| ((self.issued + 1) << 2) | TAG)
    }

    pub(crate) fn insert(&mut self, token: u64, value: T) {
        debug_assert_eq!(self.next(), Some(token));
        self.issued += 1;
        self.live.insert(token, value);
    }

    pub(crate) fn retired(&self, token: u64) -> bool {
        let ordinal = token >> 2;
        token & 3 == TAG
            && ordinal != 0
            && ordinal <= self.issued
            && !self.live.contains_key(&token)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn compact_history_distinguishes_live_retired_unissued_and_wrong_type() {
        let mut jobs = Registry::<_, 1>::new();
        let mut files = Registry::<_, 3>::new();
        let old = jobs.next().unwrap();
        assert!(!jobs.retired(old)); // A failed OS allocation issues no token.
        jobs.insert(old, 11);
        assert!(!jobs.retired(old));
        let file = files.next().unwrap();
        files.insert(file, 11);
        files.live.remove(&file);
        for _ in 0..10_000 {
            let token = jobs.next().unwrap();
            jobs.insert(token, 11); // Even when the kernel reuses a handle.
            jobs.live.remove(&token);
            assert!(jobs.retired(token));
            assert!(!files.retired(token));
        }
        assert_eq!(jobs.live.len(), 1);
        assert!(!jobs.retired(old));
        jobs.live.remove(&old);
        assert!(jobs.retired(old));
        assert!(!jobs.retired(file));
        assert!(!jobs.retired(jobs.next().unwrap()));
        assert!(!jobs.retired(1));
    }
}
