#pragma once

#include <cmath>
#include <Eigen/Dense>

inline float deg2rad(float d) {
    const float PI = std::acos(-1.0f);
    return d * PI / 180.0f;
}

inline Eigen::Affine3f makeT(float r) {
    Eigen::Affine3f T = Eigen::Affine3f::Identity();
    T.translate(Eigen::Vector3f(0.0f, 0.0f, r));
    return T;
}

inline Eigen::Affine3f makeA1(float alpha1) {
    Eigen::Affine3f T = Eigen::Affine3f::Identity();
    T.rotate(Eigen::AngleAxisf(alpha1, Eigen::Vector3f::UnitX()));
    return T;
}

inline Eigen::Affine3f makeA2(float alpha2) {
    Eigen::Affine3f T = Eigen::Affine3f::Identity();
    T.rotate(Eigen::AngleAxisf(alpha2, Eigen::Vector3f::UnitZ()));
    return T;
}

// There is no T4

inline Eigen::Affine3f makeVM(float angle) {
    Eigen::Affine3f T = Eigen::Affine3f::Identity();
    T.rotate(Eigen::AngleAxisf(angle, Eigen::Vector3f::UnitY()));
    return T;
}

inline Eigen::Affine3f makeFrameTransform(float r, float alpha1, float alpha2, float angle) {
    return makeVM(angle) * makeA2(alpha2) * makeA1(alpha1) * makeT(r);
}
