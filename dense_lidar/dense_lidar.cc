#define NOMINMAX

#include <atomic>
#include <chrono>
#include <cstdio>
#include <filesystem>
#include <iostream>
#include <string>
#include <thread>
#include <utility>
#include <vector>

#include <cxxopts.hpp>

#include "hesai_lidar_sdk.hpp"
#include "pcd_writer.h"

namespace {

using dense_lidar::Field;
using dense_lidar::Point;
using Frame = hesai::lidar::LidarDecodedFrame<Point>;

// 命令行解析后的完整运行配置，后续各阶段只读。
struct Options {
  std::filesystem::path pcap_path;
  std::filesystem::path correction_path;
  std::filesystem::path firetimes_path;
  bool has_firetimes = false;
  std::string out_folder;
  std::string file_prefix;
  std::vector<Field> fields;
  bool binary = false;
  bool play_synchronization = true;
};

enum class ParseStatus { kOk, kExit, kError };

ParseStatus ParseOptions(int argc, char* argv[], Options* options) {
  cxxopts::Options cli(argv[0], "Hesai pcap -> pcd converter with selectable fields");
  cli.set_width(100).set_tab_expansion();
  cli.add_options()
      ("p,pcap", "PCAP file path (required)", cxxopts::value<std::string>())
      ("c,correction", "Angle correction file path (required)", cxxopts::value<std::string>())
      ("t,firetimes", "Firetime file path (optional)", cxxopts::value<std::string>())
      ("o,out-folder", "Output folder (default: the pcap's folder)", cxxopts::value<std::string>())
      ("f,fields",
       "Comma separated fields: ring,x,y,z,intensity,timestamp,distance,confidence,"
       "azimuthCalib,elevationCalib",
       cxxopts::value<std::string>()->default_value("x,y,z"))
      ("b,binary", "Write binary PCD instead of ascii", cxxopts::value<bool>()->default_value("false"))
      ("no-sync", "Parse as fast as possible instead of following pcap timestamps",
       cxxopts::value<bool>()->default_value("false"))
      ("h,help", "Print help message");

  auto result = cli.parse(argc, argv);
  if (result.count("help") != 0) {
    std::cout << cli.help() << std::endl;
    return ParseStatus::kExit;
  }
  if (result.count("pcap") == 0) {
    std::cerr << "PCAP file path is required.\n" << cli.help() << std::endl;
    return ParseStatus::kError;
  }
  if (result.count("correction") == 0) {
    std::cerr << "Angle correction file path is required.\n" << cli.help() << std::endl;
    return ParseStatus::kError;
  }

  options->pcap_path = result["pcap"].as<std::string>();
  options->correction_path = result["correction"].as<std::string>();
  if (!std::filesystem::exists(options->pcap_path)) {
    std::cerr << "PCAP file not found: " << options->pcap_path.string() << std::endl;
    return ParseStatus::kError;
  }
  if (!std::filesystem::exists(options->correction_path)) {
    std::cerr << "Correction file not found: " << options->correction_path.string() << std::endl;
    return ParseStatus::kError;
  }

  std::string error;
  if (!dense_lidar::ParseFields(result["fields"].as<std::string>(), &options->fields, &error)) {
    std::cerr << "Invalid --fields: " << error << std::endl;
    return ParseStatus::kError;
  }

  if (result.count("out-folder") != 0) {
    options->out_folder = result["out-folder"].as<std::string>();
  } else {
    options->out_folder = options->pcap_path.parent_path().string();
    if (options->out_folder.empty()) {
      options->out_folder = ".";
    }
  }
  std::filesystem::create_directories(options->out_folder);
  options->file_prefix =
      options->pcap_path.stem().string().empty() ? "frame" : options->pcap_path.stem().string();

  if (result.count("firetimes") != 0) {
    options->firetimes_path = result["firetimes"].as<std::string>();
    options->has_firetimes = true;
  }
  options->binary = result["binary"].as<bool>();
  options->play_synchronization = !result["no-sync"].as<bool>();
  return ParseStatus::kOk;
}

// 逐帧写出 PCD。写出参数在构造时固化，不再依赖文件作用域全局变量。
class FrameSink {
 public:
  FrameSink(std::string out_folder, std::string file_prefix, std::vector<Field> fields, bool binary)
      : out_folder_(std::move(out_folder)),
        file_prefix_(std::move(file_prefix)),
        fields_(std::move(fields)),
        binary_(binary) {}

  void OnFrame(const Frame& frame) {
    last_frame_us_.store(GetMicroTickCountU64());
    if (frame.points_num == 0) {
      return;
    }

    char file_name[512];
    std::snprintf(file_name, sizeof(file_name), "%s_%06d_%.6f.pcd", file_prefix_.c_str(),
                  frame.frame_index, frame.frame_start_timestamp);
    std::filesystem::path path = std::filesystem::path(out_folder_) / file_name;

    std::string error;
    if (!dense_lidar::WritePcd(path.string(), frame.points, frame.points_num, fields_, binary_,
                               &error)) {
      std::fprintf(stderr, "[dense_lidar] frame %d write failed: %s\n", frame.frame_index,
                   error.c_str());
      return;
    }

    frame_count_.fetch_add(1);
    point_count_.fetch_add(frame.points_num);
    std::printf("[dense_lidar] frame %d: %u points -> %s\n", frame.frame_index, frame.points_num,
                path.string().c_str());
  }

  uint64_t last_frame_us() const { return last_frame_us_.load(); }
  uint64_t frame_count() const { return frame_count_.load(); }
  uint64_t point_count() const { return point_count_.load(); }

 private:
  const std::string out_folder_;
  const std::string file_prefix_;
  const std::vector<Field> fields_;
  const bool binary_;

  // 回调在 SDK 线程里执行，停止线程轮询读取，因此用原子量。
  std::atomic<uint64_t> last_frame_us_{0};
  std::atomic<uint64_t> frame_count_{0};
  std::atomic<uint64_t> point_count_{0};
};

// pcap -> pcd 转换流程：初始化 SDK、跑到 pcap 结束、回收资源。
class Converter {
 public:
  explicit Converter(Options options)
      : options_(std::move(options)),
        sink_(options_.out_folder, options_.file_prefix, options_.fields, options_.binary) {}

  // 初始化失败返回 false，调用方负责退出。
  bool Init() {
    LogParameters();

    hesai::lidar::DriverParam param;
    param.input_param.source_type = hesai::lidar::DATA_FROM_PCAP;
    param.input_param.pcap_path = options_.pcap_path.string();
    param.input_param.correction_file_path = options_.correction_path.string();
    param.input_param.use_ptc_connected = false;
    // 无法识别的 pcap 会让 SDK 初始化线程一直等待，这里设置超时以便及时报错退出。
    param.input_param.recv_point_cloud_timeout = 5.0f;
    if (options_.has_firetimes) {
      param.input_param.firetimes_path = options_.firetimes_path.string();
    }
    param.decoder_param.pcap_play_in_loop = false;
    param.decoder_param.pcap_play_synchronization = options_.play_synchronization;
    param.decoder_param.enable_packet_loss_tool = false;

    if (!sdk_.Init(param)) {
      std::cerr << "SDK init failed." << std::endl;
      return false;
    }
    sdk_.RegRecvCallback([this](const Frame& frame) { sink_.OnFrame(frame); });
    sdk_.Start();
    if (sdk_.lidar_ptr_->GetInitFinish(hesai::lidar::FailInit)) {
      std::cerr << "SDK init failed, check pcap/correction path." << std::endl;
      sdk_.Stop();
      return false;
    }
    return true;
  }

  // pcap 播放结束后 SDK 不会主动退出，这里等待 EOF 且 1 秒内没有新帧再收尾。
  void RunToEnd() {
    while (!sdk_.lidar_ptr_->IsPlayEnded() || GetMicroTickCountU64() - sink_.last_frame_us() < 1000000) {
      std::this_thread::sleep_for(std::chrono::milliseconds(100));
    }
    std::this_thread::sleep_for(std::chrono::milliseconds(500));
    sdk_.Stop();

    std::printf("[dense_lidar] done: %llu frames, %llu points\n",
                static_cast<unsigned long long>(sink_.frame_count()),
                static_cast<unsigned long long>(sink_.point_count()));
  }

 private:
  void LogParameters() const {
    std::printf("[dense_lidar] pcap: %s\n", options_.pcap_path.string().c_str());
    std::printf("[dense_lidar] correction: %s\n", options_.correction_path.string().c_str());
    if (options_.has_firetimes) {
      std::printf("[dense_lidar] firetimes: %s\n", options_.firetimes_path.string().c_str());
    }
    std::printf("[dense_lidar] output folder: %s\n", options_.out_folder.c_str());
    std::printf("[dense_lidar] data format: %s\n", options_.binary ? "binary" : "ascii");
    std::printf("[dense_lidar] fields:");
    for (Field field : options_.fields) {
      std::printf(" %s", dense_lidar::FindField(field).name);
    }
    std::printf("\n");
  }

  const Options options_;
  FrameSink sink_;
  hesai::lidar::HesaiLidarSdk<Point> sdk_;
};

int Run(int argc, char* argv[]) {
  Options options;
  switch (ParseOptions(argc, argv, &options)) {
    case ParseStatus::kExit:
      return 0;
    case ParseStatus::kError:
      return 1;
    case ParseStatus::kOk:
      break;
  }

  Converter converter(std::move(options));
  if (!converter.Init()) {
    return 1;
  }
  converter.RunToEnd();
  return 0;
}

}  // namespace

int main(int argc, char* argv[]) {
  try {
    return Run(argc, argv);
  } catch (const cxxopts::exceptions::exception& e) {
    std::cerr << "Argument error: " << e.what() << std::endl;
    return 1;
  } catch (const std::exception& e) {
    std::cerr << "Fatal: " << e.what() << std::endl;
    return 1;
  }
}
