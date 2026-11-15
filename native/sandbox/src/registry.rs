// Tokens are opaque, scoped to the loaded module and tagged by resource type.
// The low two bits reserve disjoint domains: reads=0, Jobs=1, processes=2,
// ACL files=3. Every native resource API must use this shared allocation rule.
// Each issued ordinal corresponds to a successful insertion. Therefore an
// issued token missing from the live map is an exact retirement receipt.
use std::collections::BTreeMap;

pub(crate) struct Registry<T, const TAG: u64> {
    issued: u64,
    pub(crate) live: BTreeMap<u64, T>,
}

impl<T, const TAG: u64> Registry<T, TAG> {
    pub(crate) fn new() -> Self {
        const {
            assert!(TAG < 4, "resource token tag exceeds its reserved bits")
        };
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

    #[test]
    fn every_resource_domain_rejects_other_domains_after_retirement() {
        let mut reads = Registry::<_, 0>::new();
        let mut jobs = Registry::<_, 1>::new();
        let mut processes = Registry::<_, 2>::new();
        let mut files = Registry::<_, 3>::new();
        for _ in 0..100 {
            let tokens = [
                reads.next().unwrap(),
                jobs.next().unwrap(),
                processes.next().unwrap(),
                files.next().unwrap(),
            ];
            reads.insert(tokens[0], ());
            jobs.insert(tokens[1], ());
            processes.insert(tokens[2], ());
            files.insert(tokens[3], ());
            for (index, token) in tokens.into_iter().enumerate() {
                assert_eq!(reads.live.contains_key(&token), index == 0);
                assert_eq!(jobs.live.contains_key(&token), index == 1);
                assert_eq!(processes.live.contains_key(&token), index == 2);
                assert_eq!(files.live.contains_key(&token), index == 3);
            }
            reads.live.clear();
            jobs.live.clear();
            processes.live.clear();
            files.live.clear();
            for (index, token) in tokens.into_iter().enumerate() {
                assert_eq!(reads.retired(token), index == 0);
                assert_eq!(jobs.retired(token), index == 1);
                assert_eq!(processes.retired(token), index == 2);
                assert_eq!(files.retired(token), index == 3);
            }
        }
    }

    #[test]
    fn token_exhaustion_is_refused_in_every_resource_domain() {
        fn exhausted<const TAG: u64>() {
            let registry = Registry::<(), TAG> {
                issued: u64::MAX >> 2,
                live: BTreeMap::new(),
            };
            assert!(registry.next().is_none());
            assert!(registry.live.is_empty());
        }
        exhausted::<0>();
        exhausted::<1>();
        exhausted::<2>();
        exhausted::<3>();
    }
}
