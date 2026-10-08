import unittest

from abbench.memory import summarize_snapshot


class MemorySnapshotTests(unittest.TestCase):
    def test_known_quantity_and_linux_kb_units(self):
        # 1 GiB total / 400 MiB available leaves 624 MiB unavailable.
        result = summarize_snapshot("MemTotal: 1048576 kB\nMemAvailable: 409600 kB\nCached: 204800 kB\n")
        self.assertEqual(result["mem_total_bytes"], 1024 ** 3)
        self.assertEqual(result["mem_available_bytes"], 400 * 1024 ** 2)
        self.assertEqual(result["estimated_unavailable_bytes"], 624 * 1024 ** 2)
        self.assertEqual(result["available_fraction"], 400 / 1024)
        # Exact 1000 MiB / 400 MiB example has 600 MiB unavailable.
        decimal_label = summarize_snapshot("MemTotal: 1024000 kB\nMemAvailable: 409600 kB\n")
        self.assertEqual(decimal_label["estimated_unavailable_bytes"], 600 * 1024 ** 2)

    def test_missing_optional_fields_are_null_and_required_fields_are_rejected(self):
        result = summarize_snapshot("MemTotal: 1000 kB\nMemAvailable: 400 kB\n")
        for field in ("slab_bytes", "cached_bytes", "cma_total_bytes", "swap_used_bytes", "zram"):
            self.assertIsNone(result[field])
        self.assertIn("Slab", result["missing_optional_fields"])
        with self.assertRaisesRegex(ValueError, "MemAvailable"):
            summarize_snapshot("MemTotal: 1000 kB\n")
        with self.assertRaisesRegex(ValueError, "MemTotal"):
            summarize_snapshot("MemAvailable: 400 kB\n")

    def test_invalid_unit_available_range_and_duplicate_are_rejected(self):
        for text in ("MemTotal: 1000 MB\nMemAvailable: 400 kB\n",
                     "MemTotal: 1000 kB\nMemAvailable: 1001 kB\n",
                     "MemTotal: 1000 kB\nMemAvailable: 400 kB\nMemAvailable: 300 kB\n",
                     "MemTotal: 1000 kB\nMemAvailable: -1 kB\n",
                     "MemTotal: 0 kB\nMemAvailable: 0 kB\n"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                summarize_snapshot(text)

    def test_shared_cache_slab_and_swap_are_not_double_counted(self):
        text = ("MemTotal: 1000 kB\nMemAvailable: 400 kB\nMemFree: 100 kB\n"
                "Cached: 300 kB\nBuffers: 10 kB\nShmem: 50 kB\nSlab: 90 kB\n"
                "SReclaimable: 60 kB\nSUnreclaim: 30 kB\nKernelStack: 5 kB\n"
                "PageTables: 7 kB\nCmaTotal: 80 kB\nCmaFree: 20 kB\n"
                "SwapTotal: 500 kB\nSwapFree: 200 kB\nHugePages_Total: 0\n")
        before = text
        result = summarize_snapshot(text)
        self.assertEqual(text, before)
        self.assertEqual(result["estimated_unavailable_bytes"], 600 * 1024)
        self.assertEqual(result["cached_bytes"], 300 * 1024)
        self.assertEqual(result["shmem_bytes"], 50 * 1024)
        self.assertEqual(result["slab_bytes"], 90 * 1024)
        self.assertEqual(result["sreclaimable_bytes"], 60 * 1024)
        self.assertEqual(result["swap_used_bytes"], 300 * 1024)
        self.assertEqual(result["raw_meminfo_fields"]["HugePages_Total"]["unit"], None)
        self.assertNotIn("process_total_bytes", result)

    def test_zram_standard_prefix_is_preserved_but_not_claimed_verified(self):
        meminfo = "MemTotal: 1000 kB\nMemAvailable: 400 kB\n"
        result = summarize_snapshot(meminfo, "307200 102400 131072 0 0 12 1 2 0\n")
        self.assertEqual(result["zram"]["orig_data_size_bytes"], 307200)
        self.assertEqual(result["zram"]["compr_data_size_bytes"], 102400)
        self.assertEqual(result["zram"]["mem_used_total_bytes"], 131072)
        self.assertFalse(result["zram"]["schema_verified"])
        self.assertEqual(result["estimated_unavailable_bytes"], 600 * 1024)
        for bad in ("", "1 2", "1 -2 3", "1 2 x"):
            with self.subTest(mm_stat=bad), self.assertRaises(ValueError):
                summarize_snapshot(meminfo, bad)


if __name__ == "__main__":
    unittest.main()
