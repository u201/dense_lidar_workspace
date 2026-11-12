#define NOMINMAX

#include <atomic>
#include <chrono>
#include <cstdio>
#include <filesystem>
#include <iostream>
#include <string>
#include <thread>
#include <vector>

#include <cxxopts.hpp>

#include "hesai_lidar_sdk.hpp"
#include "pcd_writer.h"

namespace {

using dense_lidar::Field;
using dense_lidar::Point;

std::atomic<uint64_t> g_last_frame_us{0};
std::atomic<uint64_t> g_frame_count{0};
std::atomic<uint64_t> g_point_count{0};

std::string g_out_folder;
std::string g_file_prefix;
std::vector<Field> g_fields;
bool g_binary = false;

void OnFrame(const hesai::lidar::LidarDecodedFrame<Point>& frame) {
  g_last_frame_us.store(GetMicroTickCountU64());
  if (frame.points_num == 0) {
    return;
  }

  char file_name[512];
  std::snprintf(file_name, sizeof(file_name), "%s_%06d_%.6f.pcd", g_file_prefix.c_str(),
                frame.frame_index, frame.frame_start_timestamp);
  std::filesystem::path path = std::filesystem::path(g_out_folder) / file_name;

  std::string error;
  if (!dense_lidar::WritePcd(path.string(), frame.points, frame.points_num, g_fields, g_binary,
                             &error)) {
    std::fprintf(stderr, "[dense_lidar] frame %d write failed: %s\n", frame.frame_index,
                 error.c_str());
    return;
  }

  g_frame_count.fetch_add(1);
  g_point_count.fetch_add(frame.points_num);
  std::printf("[dense_lidar] frame %d: %u points -> %s\n", frame.frame_index, frame.points_num,
              path.string().c_str());
}

int Run(int argc, char* argv[]) {
  cxxopts::Options options(argv[0], "Hesai pcap -> pcd converter with selectable fields");
  options.set_width(100).set_tab_expansion();
  options.add_options()
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

  auto result = options.parse(argc, argv);
  if (result.count("help") != 0) {
    std::cout << options.help() << std::endl;
    return 0;
  }
  if (result.count("pcap") == 0) {
    std::cerr << "PCAP file path is required.\n" << options.help() << std::endl;
    return 1;
  }
  if (result.count("correction") == 0) {
    std::cerr << "Angle correction file path is required.\n" << options.help() << std::endl;
    return 1;
  }

  const std::filesystem::path pcap_path(result["pcap"].as<std::string>());
  const std::filesystem::path correction_path(result["correction"].as<std::string>());
  if (!std::filesystem::exists(pcap_path)) {
    std::cerr << "PCAP file not found: " << pcap_path.string() << std::endl;
    return 1;
  }
  if (!std::filesystem::exists(correction_path)) {
    std::cerr << "Correction file not found: " << correction_path.string() << std::endl;
    return 1;
  }

  std::string error;
  if (!dense_lidar::ParseFields(result["fields"].as<std::string>(), &g_fields, &error)) {
    std::cerr << "Invalid --fields: " << error << std::endl;
    return 1;
  }

  std::filesystem::path out_folder;
  if (result.count("out-folder") != 0) {
    out_folder = result["out-folder"].as<std::string>();
  } else {
    out_folder = pcap_path.parent_path();
    if (out_folder.empty()) {
      out_folder = ".";
    }
  }
  std::filesystem::create_directories(out_folder);
  g_out_folder = out_folder.string();
  g_file_prefix = pcap_path.stem().string().empty() ? "frame" : pcap_path.stem().string();
  g_binary = result["binary"].as<bool>();

  std::printf("[dense_lidar] pcap: %s\n", pcap_path.string().c_str());
  std::printf("[dense_lidar] correction: %s\n", correction_path.string().c_str());
  std::printf("[dense_lidar] output folder: %s\n", g_out_folder.c_str());
  std::printf("[dense_lidar] data format: %s\n", g_binary ? "binary" : "ascii");
  std::printf("[dense_lidar] fields:");
  for (Field field : g_fields) {
    for (const dense_lidar::FieldInfo& info : dense_lidar::AllFields()) {
      if (info.field == field) {
        std::printf(" %s", info.name);
        break;
      }
    }
  }
  std::printf("\n");

  hesai::lidar::DriverParam param;
  param.input_param.source_type = hesai::lidar::DATA_FROM_PCAP;
  param.input_param.pcap_path = pcap_path.string();
  param.input_param.correction_file_path = correction_path.string();
  param.input_param.use_ptc_connected = false;
  // 无法识别的 pcap 会让 SDK 初始化线程一直等待，这里设置超时以便及时报错退出。
  param.input_param.recv_point_cloud_timeout = 5.0f;
  if (result.count("firetimes") != 0) {
    param.input_param.firetimes_path = result["firetimes"].as<std::string>();
    std::printf("[dense_lidar] firetimes: %s\n", param.input_param.firetimes_path.c_str());
  }
  param.decoder_param.pcap_play_in_loop = false;
  param.decoder_param.pcap_play_synchronization = !result["no-sync"].as<bool>();
  param.decoder_param.enable_packet_loss_tool = false;

  hesai::lidar::HesaiLidarSdk<Point> sdk;
  sdk.Init(param);
  sdk.RegRecvCallback(OnFrame);
  sdk.Start();
  if (sdk.lidar_ptr_->GetInitFinish(hesai::lidar::FailInit)) {
    std::cerr << "SDK init failed, check pcap/correction path." << std::endl;
    sdk.Stop();
    return 1;
  }

  // pcap 播放结束后 SDK 不会主动退出，这里等待 EOF 且 1 秒内没有新帧再收尾。
  while (!sdk.lidar_ptr_->IsPlayEnded() ||
         GetMicroTickCountU64() - g_last_frame_us.load() < 1000000) {
    std::this_thread::sleep_for(std::chrono::milliseconds(100));
  }
  std::this_thread::sleep_for(std::chrono::milliseconds(500));
  sdk.Stop();

  std::printf("[dense_lidar] done: %llu frames, %llu points\n",
              static_cast<unsigned long long>(g_frame_count.load()),
              static_cast<unsigned long long>(g_point_count.load()));
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
