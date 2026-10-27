import {test, expect} from "bun:test";
import {parseChecks, formatChecks} from "./publishChecks.js";

test("editing checks preserves exact stable IDs and reproducible seed/timeout settings", () => {
  const prior = [{id: "smoke", target: "tests/test_api.py::test_read", seeds: [42, 7], timeout: 30}];
  expect(parseChecks(formatChecks(prior), prior)).toEqual(prior);
  expect(parseChecks("")).toEqual([]);
  expect(parseChecks("tests/test_api.py")).toEqual([{target: "tests/test_api.py", seeds: [42], timeout: 30}]);
});

test("invalid matrix input is rejected before saving settings or a draft", () => {
  for (const value of ["test.py | | 30", "test.py | 42,42 | 30", "test.py | -1 | 30",
    "test.py | 42 | 0", "test.py | 42,7 | 120", "test.py;", "test.py | 42 | 30 | unexpected"])
    expect(() => parseChecks(value)).toThrow();
});
