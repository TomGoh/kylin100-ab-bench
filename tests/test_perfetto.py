import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from abbench.perfetto import assess_powerstats, export_trace

INVENTORY = ('track_id,name,type,unit,source_arg_set_id,dimension_arg_set_id,sample_count,first_ts_ns,last_ts_ns,min_value,max_value\n'
             '0,batt.current_ua,battery_counter,[NULL],[NULL],0,2,1000000000,2000000000,-500,-500\n')
SAMPLES = ('track_id,name,type,unit,source_arg_set_id,dimension_arg_set_id,ts_ns,value\n'
           '0,batt.current_ua,battery_counter,[NULL],[NULL],0,1000000000,-500\n')


class PerfettoTests(unittest.TestCase):
    def test_example_hal_is_excluded_even_when_channels_exist(self):
        result = assess_powerstats("771 android.hardware.power.stats-service.example", "ChannelId: 0 Rail1")
        self.assertTrue(result["example_service_detected"])
        self.assertFalse(result["rail_measurement_validated"])
        self.assertEqual(result["reason"], "example_service_fake_data")

    def test_real_named_service_is_not_automatically_validated(self):
        result = assess_powerstats("771 platform-power-meter", "ChannelId: 0 CPU")
        self.assertFalse(result["example_service_detected"])
        self.assertFalse(result["rail_measurement_validated"])

    def fixture(self, directory):
        trace, processor = Path(directory) / "trace", Path(directory) / "processor"
        trace.write_bytes(b"trace")
        processor.write_bytes(b"processor")
        return trace, processor

    def test_official_tool_exports_without_claiming_measurement_validity(self):
        with tempfile.TemporaryDirectory() as directory:
            trace, processor = self.fixture(directory)
            responses = [subprocess.CompletedProcess([], 0, "Perfetto v58.2", ""),
                         subprocess.CompletedProcess([], 0, INVENTORY, "log"),
                         subprocess.CompletedProcess([], 0, SAMPLES, "log")]
            with patch("abbench.perfetto.subprocess.run", side_effect=responses) as run:
                result = export_trace(trace, processor, Path(directory) / "export")
            self.assertFalse(result["measurement_validated"])
            self.assertEqual(result["tables"]["counter-inventory"]["rows"], 1)
            self.assertIn("-q", run.call_args_list[1].args[0])
            with self.assertRaises(FileExistsError):
                export_trace(trace, processor, Path(directory) / "export")

    def test_empty_telemetry_is_not_zero_power(self):
        with tempfile.TemporaryDirectory() as directory:
            trace, processor = self.fixture(directory)
            responses = [subprocess.CompletedProcess([], 0, "version", ""),
                         subprocess.CompletedProcess([], 0, INVENTORY.splitlines()[0] + '\n', "")]
            with patch("abbench.perfetto.subprocess.run", side_effect=responses):
                with self.assertRaisesRegex(ValueError, "no counter"):
                    export_trace(trace, processor, Path(directory) / "export")
            self.assertTrue((Path(directory) / "export/failure.json").exists())

    def test_processor_failure_cannot_be_empty_success(self):
        with tempfile.TemporaryDirectory() as directory:
            trace, processor = self.fixture(directory)
            with patch("abbench.perfetto.subprocess.run", side_effect=subprocess.CalledProcessError(1, ["processor"])):
                with self.assertRaises(ValueError):
                    export_trace(trace, processor, Path(directory) / "export")

    def test_wrong_schema_and_nonfinite_samples_are_not_successful_exports(self):
        for output, table in [("name\nnot-a-counter\n", "inventory"),
                              (INVENTORY.replace(',2,1000000000', ',0,1000000000'), "inventory"),
                              (SAMPLES.replace(',-500\n', ',nan\n'), "samples")]:
            with self.subTest(table=table), tempfile.TemporaryDirectory() as directory:
                trace, processor = self.fixture(directory)
                responses = [subprocess.CompletedProcess([], 0, "version", "")]
                if table == "samples":
                    responses.append(subprocess.CompletedProcess([], 0, INVENTORY, ""))
                responses.append(subprocess.CompletedProcess([], 0, output, ""))
                with patch("abbench.perfetto.subprocess.run", side_effect=responses):
                    with self.assertRaises(ValueError):
                        export_trace(trace, processor, Path(directory) / "export")
                self.assertFalse((Path(directory) / "export/export.json").exists())
                self.assertTrue((Path(directory) / "export/failure.json").exists())


if __name__ == "__main__":
    unittest.main()
