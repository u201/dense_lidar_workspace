#include "pcd_writer.h"

#include <algorithm>
#include <cstdio>
#include <fstream>

namespace dense_lidar {
namespace {

// 缓冲区超过该阈值就落盘一次，避免整帧数据常驻内存。
constexpr size_t kFlushThreshold = 1u << 20;
// WIDTH / POINTS 的定宽位数，uint64 最多 20 位。
constexpr int kCountWidth = 20;

// 每个字段一行：元信息 + ascii 格式化 + binary 取址。
// 新增字段只需在这里加一条，ascii / binary 两条写出路径共用，避免两份 switch 漂移。
struct FieldSpec {
  FieldInfo info;
  void (*FormatAscii)(const Point&, char* buf, size_t len);
  const void* (*Raw)(const Point&);
};

const std::vector<FieldSpec>& FieldTable() {
  static const std::vector<FieldSpec> kTable = {
      {{Field::kRing, "ring", 2, 'U'},
       [](const Point& p, char* buf, size_t len) {
         std::snprintf(buf, len, "%u", static_cast<unsigned>(p.ring));
       },
       [](const Point& p) -> const void* { return &p.ring; }},
      {{Field::kX, "x", 4, 'F'},
       [](const Point& p, char* buf, size_t len) { std::snprintf(buf, len, "%.9g", p.x); },
       [](const Point& p) -> const void* { return &p.x; }},
      {{Field::kY, "y", 4, 'F'},
       [](const Point& p, char* buf, size_t len) { std::snprintf(buf, len, "%.9g", p.y); },
       [](const Point& p) -> const void* { return &p.y; }},
      {{Field::kZ, "z", 4, 'F'},
       [](const Point& p, char* buf, size_t len) { std::snprintf(buf, len, "%.9g", p.z); },
       [](const Point& p) -> const void* { return &p.z; }},
      {{Field::kIntensity, "intensity", 1, 'U'},
       [](const Point& p, char* buf, size_t len) {
         std::snprintf(buf, len, "%u", static_cast<unsigned>(p.intensity));
       },
       [](const Point& p) -> const void* { return &p.intensity; }},
      {{Field::kTimestamp, "timestamp", 8, 'F'},
       // 时间戳通常是 1e9 量级的秒数，用 17 位有效数字避免精度损失。
       [](const Point& p, char* buf, size_t len) { std::snprintf(buf, len, "%.17g", p.timestamp); },
       [](const Point& p) -> const void* { return &p.timestamp; }},
      {{Field::kDistance, "distance", 4, 'F'},
       [](const Point& p, char* buf, size_t len) { std::snprintf(buf, len, "%.9g", p.distance); },
       [](const Point& p) -> const void* { return &p.distance; }},
      {{Field::kConfidence, "confidence", 1, 'U'},
       [](const Point& p, char* buf, size_t len) {
         std::snprintf(buf, len, "%u", static_cast<unsigned>(p.confidence));
       },
       [](const Point& p) -> const void* { return &p.confidence; }},
      {{Field::kAzimuthCalib, "azimuthCalib", 4, 'F'},
       [](const Point& p, char* buf, size_t len) {
         std::snprintf(buf, len, "%.9g", p.azimuthCalib);
       },
       [](const Point& p) -> const void* { return &p.azimuthCalib; }},
      {{Field::kElevationCalib, "elevationCalib", 4, 'F'},
       [](const Point& p, char* buf, size_t len) {
         std::snprintf(buf, len, "%.9g", p.elevationCalib);
       },
       [](const Point& p) -> const void* { return &p.elevationCalib; }},
  };
  return kTable;
}

const FieldSpec& SpecOf(Field field) {
  const std::vector<FieldSpec>& table = FieldTable();
  for (const FieldSpec& spec : table) {
    if (spec.info.field == field) {
      return spec;
    }
  }
  // Field 枚举值都被上表覆盖，这里不会到达。
  return table.front();
}

void AppendField(std::string* out, const Point& point, const FieldSpec& spec, bool binary) {
  if (binary) {
    out->append(static_cast<const char*>(spec.Raw(point)), spec.info.size);
    return;
  }
  char buf[64];
  spec.FormatAscii(point, buf, sizeof(buf));
  out->append(buf);
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

const FieldInfo& FindField(Field field) { return SpecOf(field).info; }

bool ParseFields(const std::string& text, std::vector<Field>* fields, std::string* error) {
  fields->clear();
  size_t pos = 0;
  while (pos <= text.size()) {
    size_t comma = text.find(',', pos);
    std::string name = Trim(text.substr(pos, comma == std::string::npos ? std::string::npos : comma - pos));
    if (!name.empty()) {
      bool found = false;
      for (const FieldSpec& spec : FieldTable()) {
        if (name == spec.info.name) {
          found = true;
          if (std::find(fields->begin(), fields->end(), spec.info.field) == fields->end()) {
            fields->push_back(spec.info.field);
          }
          break;
        }
      }
      if (!found) {
        std::string supported;
        for (const FieldSpec& spec : FieldTable()) {
          supported += supported.empty() ? spec.info.name : std::string(",") + spec.info.name;
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

PcdWriter::~PcdWriter() {
  if (out_.is_open()) {
    Close(nullptr);
  }
}

bool PcdWriter::Open(const std::string& path, const std::vector<Field>& fields, bool binary,
                     std::string* error) {
  if (out_.is_open()) {
    out_.close();
  }
  path_ = path;
  fields_ = fields;
  binary_ = binary;
  point_count_ = 0;
  buffer_.clear();
  buffer_.reserve(kFlushThreshold + 256);

  out_.open(path, std::ios::binary | std::ios::trunc);
  if (!out_) {
    if (error != nullptr) {
      *error = "failed to open " + path;
    }
    return false;
  }
  if (!WriteHeader(0)) {
    if (error != nullptr) {
      *error = "failed to write " + path;
    }
    out_.close();
    return false;
  }
  return true;
}

bool PcdWriter::Append(const Point* points, size_t count, std::string* error) {
  if (!out_.is_open()) {
    if (error != nullptr) {
      *error = "writer is not open";
    }
    return false;
  }
  if (count == 0) {
    return true;
  }

  // 每帧查找一次字段，点循环里只按下标访问，避免逐点线性查表。
  std::vector<const FieldSpec*> columns;
  columns.reserve(fields_.size());
  for (Field field : fields_) {
    columns.push_back(&SpecOf(field));
  }

  for (size_t i = 0; i < count; ++i) {
    for (size_t j = 0; j < columns.size(); ++j) {
      if (!binary_ && j != 0) {
        buffer_.push_back(' ');
      }
      AppendField(&buffer_, points[i], *columns[j], binary_);
    }
    if (!binary_) {
      buffer_.push_back('\n');
    }
    if (buffer_.size() >= kFlushThreshold) {
      out_.write(buffer_.data(), static_cast<std::streamsize>(buffer_.size()));
      buffer_.clear();
      if (!out_) {
        if (error != nullptr) {
          *error = "failed to write " + path_;
        }
        return false;
      }
    }
  }
  point_count_ += count;
  return true;
}

bool PcdWriter::Close(std::string* error) {
  if (!out_.is_open()) {
    if (error != nullptr) {
      *error = "writer is not open";
    }
    return false;
  }

  if (!buffer_.empty()) {
    out_.write(buffer_.data(), static_cast<std::streamsize>(buffer_.size()));
    buffer_.clear();
  }
  out_.flush();
  if (!out_) {
    if (error != nullptr) {
      *error = "failed to write " + path_;
    }
    out_.close();
    return false;
  }

  // 头部占位与回填使用同一套定宽格式，重写后长度不变，后面的数据无需搬动。
  out_.seekp(0);
  if (!WriteHeader(point_count_)) {
    if (error != nullptr) {
      *error = "failed to write " + path_;
    }
    out_.close();
    return false;
  }
  out_.flush();
  out_.close();
  if (!out_) {
    if (error != nullptr) {
      *error = "failed to write " + path_;
    }
    return false;
  }
  return true;
}

bool PcdWriter::WriteHeader(uint64_t count) {
  char count_text[kCountWidth + 1];
  std::snprintf(count_text, sizeof(count_text), "%0*llu", kCountWidth,
                static_cast<unsigned long long>(count));

  out_ << "# .PCD v0.7 - Point Cloud Data file format\n";
  out_ << "VERSION 0.7\n";

  out_ << "FIELDS";
  for (Field field : fields_) {
    out_ << ' ' << SpecOf(field).info.name;
  }
  out_ << "\nSIZE";
  for (Field field : fields_) {
    out_ << ' ' << static_cast<unsigned>(SpecOf(field).info.size);
  }
  out_ << "\nTYPE";
  for (Field field : fields_) {
    out_ << ' ' << SpecOf(field).info.type;
  }
  out_ << "\nCOUNT";
  for (size_t i = 0; i < fields_.size(); ++i) {
    out_ << " 1";
  }
  out_ << "\nWIDTH " << count_text << "\n";
  out_ << "HEIGHT 1\n";
  out_ << "VIEWPOINT 0 0 0 1 0 0 0\n";
  out_ << "POINTS " << count_text << "\n";
  out_ << "DATA " << (binary_ ? "binary" : "ascii") << "\n";
  return static_cast<bool>(out_);
}

bool WritePcd(const std::string& path, const Point* points, size_t count,
              const std::vector<Field>& fields, bool binary, std::string* error) {
  PcdWriter writer;
  if (!writer.Open(path, fields, binary, error)) {
    return false;
  }
  if (!writer.Append(points, count, error)) {
    return false;
  }
  return writer.Close(error);
}

}  // namespace dense_lidar
