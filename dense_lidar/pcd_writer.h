#pragma once

#include <cstddef>
#include <cstdint>
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

const std::vector<FieldInfo>& AllFields();

// 解析逗号分隔的字段列表，忽略空白并去重；遇到未知字段返回 false。
bool ParseFields(const std::string& text, std::vector<Field>* fields, std::string* error);

// 按选中字段动态生成 PCD 头和数据（ascii / binary），不依赖 PCL。
bool WritePcd(const std::string& path, const Point* points, size_t count,
              const std::vector<Field>& fields, bool binary, std::string* error);

}  // namespace dense_lidar
