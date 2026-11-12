#pragma once

#include <cstddef>
#include <cstdint>
#include <fstream>
#include <string>
#include <vector>

namespace dense_lidar {

// 字段名必须与 Hesai SDK 解析器里的成员名一致（SDK 通过 SFINAE 判断成员是否存在），
// 结构体包含全部字段后，各版本 UDP 解析器只会填充自己支持的字段，其余保持 0。
#pragma pack(push, 1)
struct Point {
  float x = 0.0f;
  float y = 0.0f;
  float z = 0.0f;
  uint8_t intensity = 0;
  uint16_t ring = 0;
  double timestamp = 0.0;
  float distance = 0.0f;
  uint8_t confidence = 0;
  float azimuthCalib = 0.0f;
  float elevationCalib = 0.0f;
};
#pragma pack(pop)

enum class Field : uint8_t {
  kRing,
  kX,
  kY,
  kZ,
  kIntensity,
  kTimestamp,
  kDistance,
  kConfidence,
  kAzimuthCalib,
  kElevationCalib,
};

struct FieldInfo {
  Field field;
  const char* name;
  uint8_t size;
  char type;  // 'F' float, 'U' unsigned integer
};

// 返回字段的 PCD 元信息；枚举值全覆盖，正常调用不会失败。
const FieldInfo& FindField(Field field);

// 解析逗号分隔的字段列表，忽略空白并去重；遇到未知字段返回 false。
bool ParseFields(const std::string& text, std::vector<Field>* fields, std::string* error);

// 流式 PCD 写出器：Open 时写出定宽占位头（WIDTH / POINTS 为 20 位零填充），
// Append 可多次追加不同帧的点，Close 时回填真实点数。
// 定宽保证重写头部不会改变文件长度，后面的数据无需搬动。
// 非线程安全，同一实例只应由单一线程使用。
class PcdWriter {
 public:
  PcdWriter() = default;
  ~PcdWriter();

  PcdWriter(const PcdWriter&) = delete;
  PcdWriter& operator=(const PcdWriter&) = delete;

  // 创建并截断文件，写出头部占位。
  bool Open(const std::string& path, const std::vector<Field>& fields, bool binary,
            std::string* error);

  // 追加一批点，可在 Open 与 Close 之间调用多次。count 为 0 时直接返回 true。
  bool Append(const Point* points, size_t count, std::string* error);

  // 落盘剩余数据，回填头部真实 WIDTH / POINTS。未 Open 时返回 false。
  bool Close(std::string* error);

 private:
  // 按 count 写完整头部；Open 阶段传 0 作为占位。
  bool WriteHeader(uint64_t count);

  std::ofstream out_;
  std::string path_;
  std::vector<Field> fields_;
  std::string buffer_;
  uint64_t point_count_ = 0;
  bool binary_ = false;
};

// 一次性写出单帧；内部复用 PcdWriter，等价于 Open + Append + Close。
bool WritePcd(const std::string& path, const Point* points, size_t count,
              const std::vector<Field>& fields, bool binary, std::string* error);

}  // namespace dense_lidar
