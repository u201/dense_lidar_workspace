#include "pcd_writer.h"

#include <algorithm>
#include <cstdio>
#include <fstream>

namespace dense_lidar {
namespace {

const FieldInfo& InfoOf(Field field) {
  for (const FieldInfo& info : AllFields()) {
    if (info.field == field) {
      return info;
    }
  }
  // AllFields() 覆盖所有枚举值，这里不会到达。
  static const FieldInfo kUnknown{Field::kX, "x", 4, 'F'};
  return kUnknown;
}

void AppendAscii(std::string* out, const Point& point, Field field) {
  char buf[64];
  switch (field) {
    case Field::kRing:
      std::snprintf(buf, sizeof(buf), "%u", static_cast<unsigned>(point.ring));
      break;
    case Field::kX:
      std::snprintf(buf, sizeof(buf), "%.9g", point.x);
      break;
    case Field::kY:
      std::snprintf(buf, sizeof(buf), "%.9g", point.y);
      break;
    case Field::kZ:
      std::snprintf(buf, sizeof(buf), "%.9g", point.z);
      break;
    case Field::kIntensity:
      std::snprintf(buf, sizeof(buf), "%u", static_cast<unsigned>(point.intensity));
      break;
    case Field::kTimestamp:
      // 时间戳通常是 1e9 量级的秒数，用 17 位有效数字避免精度损失。
      std::snprintf(buf, sizeof(buf), "%.17g", point.timestamp);
      break;
    case Field::kDistance:
      std::snprintf(buf, sizeof(buf), "%.9g", point.distance);
      break;
    case Field::kConfidence:
      std::snprintf(buf, sizeof(buf), "%u", static_cast<unsigned>(point.confidence));
      break;
    case Field::kAzimuthCalib:
      std::snprintf(buf, sizeof(buf), "%.9g", point.azimuthCalib);
      break;
    case Field::kElevationCalib:
      std::snprintf(buf, sizeof(buf), "%.9g", point.elevationCalib);
      break;
  }
  out->append(buf);
}

void AppendBinary(std::string* out, const Point& point, Field field) {
  switch (field) {
    case Field::kRing:
      out->append(reinterpret_cast<const char*>(&point.ring), sizeof(point.ring));
      break;
    case Field::kX:
      out->append(reinterpret_cast<const char*>(&point.x), sizeof(point.x));
      break;
    case Field::kY:
      out->append(reinterpret_cast<const char*>(&point.y), sizeof(point.y));
      break;
    case Field::kZ:
      out->append(reinterpret_cast<const char*>(&point.z), sizeof(point.z));
      break;
    case Field::kIntensity:
      out->append(reinterpret_cast<const char*>(&point.intensity), sizeof(point.intensity));
      break;
    case Field::kTimestamp:
      out->append(reinterpret_cast<const char*>(&point.timestamp), sizeof(point.timestamp));
      break;
    case Field::kDistance:
      out->append(reinterpret_cast<const char*>(&point.distance), sizeof(point.distance));
      break;
    case Field::kConfidence:
      out->append(reinterpret_cast<const char*>(&point.confidence), sizeof(point.confidence));
      break;
    case Field::kAzimuthCalib:
      out->append(reinterpret_cast<const char*>(&point.azimuthCalib), sizeof(point.azimuthCalib));
      break;
    case Field::kElevationCalib:
      out->append(reinterpret_cast<const char*>(&point.elevationCalib), sizeof(point.elevationCalib));
      break;
  }
}

std::string Trim(const std::string& text) {
  const char* whitespace = " \t\r\n";
  size_t begin = text.find_first_not_of(whitespace);
  if (begin == std::string::npos) {
    return std::string();
  }
  size_t end = text.find_last_not_of(whitespace);
  return text.substr(begin, end - begin + 1);
}

}  // namespace

const std::vector<FieldInfo>& AllFields() {
  static const std::vector<FieldInfo> kFields = {
      {Field::kRing, "ring", 2, 'U'},
      {Field::kX, "x", 4, 'F'},
      {Field::kY, "y", 4, 'F'},
      {Field::kZ, "z", 4, 'F'},
      {Field::kIntensity, "intensity", 1, 'U'},
      {Field::kTimestamp, "timestamp", 8, 'F'},
      {Field::kDistance, "distance", 4, 'F'},
      {Field::kConfidence, "confidence", 1, 'U'},
      {Field::kAzimuthCalib, "azimuthCalib", 4, 'F'},
      {Field::kElevationCalib, "elevationCalib", 4, 'F'},
  };
  return kFields;
}

bool ParseFields(const std::string& text, std::vector<Field>* fields, std::string* error) {
  fields->clear();
  size_t pos = 0;
  while (pos <= text.size()) {
    size_t comma = text.find(',', pos);
    std::string name = Trim(text.substr(pos, comma == std::string::npos ? std::string::npos : comma - pos));
    if (!name.empty()) {
      bool found = false;
      for (const FieldInfo& info : AllFields()) {
        if (name == info.name) {
          found = true;
          if (std::find(fields->begin(), fields->end(), info.field) == fields->end()) {
            fields->push_back(info.field);
          }
          break;
        }
      }
      if (!found) {
        std::string supported;
        for (const FieldInfo& info : AllFields()) {
          supported += supported.empty() ? info.name : std::string(",") + info.name;
        }
        if (error != nullptr) {
          *error = "unknown field '" + name + "', supported fields: " + supported;
        }
        return false;
      }
    }
    if (comma == std::string::npos) {
      break;
    }
    pos = comma + 1;
  }
  if (fields->empty()) {
    if (error != nullptr) {
      *error = "field list is empty";
    }
    return false;
  }
  return true;
}

bool WritePcd(const std::string& path, const Point* points, size_t count,
              const std::vector<Field>& fields, bool binary, std::string* error) {
  std::ofstream out(path, std::ios::binary | std::ios::trunc);
  if (!out) {
    if (error != nullptr) {
      *error = "failed to open " + path;
    }
    return false;
  }

  out << "# .PCD v0.7 - Point Cloud Data file format\n";
  out << "VERSION 0.7\n";

  out << "FIELDS";
  for (Field field : fields) {
    out << ' ' << InfoOf(field).name;
  }
  out << "\nSIZE";
  for (Field field : fields) {
    out << ' ' << static_cast<unsigned>(InfoOf(field).size);
  }
  out << "\nTYPE";
  for (Field field : fields) {
    out << ' ' << InfoOf(field).type;
  }
  out << "\nCOUNT";
  for (size_t i = 0; i < fields.size(); ++i) {
    out << " 1";
  }
  out << "\nWIDTH " << count << "\n";
  out << "HEIGHT 1\n";
  out << "VIEWPOINT 0 0 0 1 0 0 0\n";
  out << "POINTS " << count << "\n";
  out << "DATA " << (binary ? "binary" : "ascii") << "\n";

  constexpr size_t kFlushThreshold = 1u << 20;
  std::string buffer;
  buffer.reserve(kFlushThreshold + 256);

  if (binary) {
    for (size_t i = 0; i < count; ++i) {
      for (Field field : fields) {
        AppendBinary(&buffer, points[i], field);
      }
      if (buffer.size() >= kFlushThreshold) {
        out.write(buffer.data(), static_cast<std::streamsize>(buffer.size()));
        buffer.clear();
      }
    }
  } else {
    for (size_t i = 0; i < count; ++i) {
      for (size_t j = 0; j < fields.size(); ++j) {
        if (j != 0) {
          buffer.push_back(' ');
        }
        AppendAscii(&buffer, points[i], fields[j]);
      }
      buffer.push_back('\n');
      if (buffer.size() >= kFlushThreshold) {
        out.write(buffer.data(), static_cast<std::streamsize>(buffer.size()));
        buffer.clear();
      }
    }
  }

  if (!buffer.empty()) {
    out.write(buffer.data(), static_cast<std::streamsize>(buffer.size()));
  }
  out.flush();
  if (!out) {
    if (error != nullptr) {
      *error = "failed to write " + path;
    }
    return false;
  }
  return true;
}

}  // namespace dense_lidar
