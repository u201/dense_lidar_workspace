
#include "transform.h"

#include <iostream>
#include <cmath>
#include <Eigen/Dense>

bool isEqual(Eigen::Affine3f a, Eigen::Affine3f b, float tol = 1e-6) {
    return a.translation().isApprox(b.translation(), tol) &&
           a.rotation().isApprox(b.rotation(), tol);
}

int main() {
    Eigen::Matrix4f matT;
    matT << 
        1.0f, 0.0f, 0.0f, 0.0f,
        0.0f, 1.0f, 0.0f, 0.0f,
        0.0f, 0.0f, 1.0f, 0.0f,
        0.0f, 0.0f, 0.0945f, 1.0f;
    Eigen::Affine3f refT(matT.transpose());
    Eigen::Matrix4f matA1;
    matA1 << 
        1.0f, 0.0f, 0.0f, 0.0f,
        0.0f, 0.999998476913288f, 0.001745328365898f, 0.0f,
        0.0f, -0.001745328365898f, 0.999998476913288f, 0.0f,
        0.0f, 0.0f, 0.0f, 1.0f;
    Eigen::Affine3f refA1(matA1.transpose());
    Eigen::Matrix4f matA2;
    matA2 << 
        0.999997386231368f, -0.002286379328088f, 0.0f, 0.0f,
        0.002286379328088f, 0.999997386231368f, 0.0f, 0.0f,
        0.0f, 0.0f, 1.0f, 0.0f,
        0.0f, 0.0f, 0.0f, 1.0f;
    Eigen::Affine3f refA2(matA2.transpose());
    Eigen::Matrix4f matVM;
    // matVM << 
        // 1.0f, 0.0f, -2.449293598294706e-16f, 0.0f,
        // 0.0f, 1.0f, 0.0f, 0.0f,
        // 2.449293598294706e-16f, 0.0f, 1.0f, 0.0f,
        // 0.0f, 0.0f, 0.0f, 1.0f;
    matVM <<
        0.500000000000000f, 0.0f, -0.866025403784439f, 0.0f,
        0.0f, 1.0f, 0.0f, 0.0f,
        0.866025403784439f, 0.0f, 0.50f, 0.0f,
        0.0f, 0.0f, 0.0f, 1.0f;
    Eigen::Affine3f refVM(matVM);
    float alpha1 = 0.1f;
    float alpha2 = -0.131f;
    float r = 0.0945f;
    // float angle = 360.0f;
    float angle = 300.0f;
    Eigen::Affine3f T = makeT(r);
    Eigen::Affine3f A1 = makeA1(deg2rad(alpha1));
    Eigen::Affine3f A2 = makeA2(deg2rad(alpha2));
    Eigen::Affine3f VM = makeVM(deg2rad(angle));
    std::cout << std::endl;
    if (isEqual(T, refT)) {
        std::cout << "T is equal to refT" << std::endl;
        std::cout << "T: " << T.translation() << std::endl;
        std::cout << "refT: " << refT.translation() << std::endl;
    } else {
        std::cout << "T is not equal to refT" << std::endl;
        std::cout << "T: " << T.translation() << std::endl;
        std::cout << "refT: " << refT.translation() << std::endl;
    }
    std::cout << std::endl;
    if (isEqual(A1, refA1)) {
        std::cout << "A1 is equal to refA1" << std::endl;
        std::cout << "A1: " << A1.rotation() << std::endl;
        std::cout << "refA1: " << refA1.rotation() << std::endl;
    } else {
        std::cout << "A1 is not equal to refA1" << std::endl;
        std::cout << "A1: " << A1.rotation() << std::endl;
        std::cout << "refA1: " << refA1.rotation() << std::endl;
    }
    std::cout << std::endl;
    if (isEqual(A2, refA2)) {
        std::cout << "A2 is equal to refA2" << std::endl;
        std::cout << "A2: " << A2.rotation() << std::endl;
        std::cout << "refA2: " << refA2.rotation() << std::endl;
    } else {
        std::cout << "A2 is not equal to refA2" << std::endl;
        std::cout << "A2: " << A2.rotation() << std::endl;
        std::cout << "refA2: " << refA2.rotation() << std::endl;
    }
    std::cout << std::endl;
    if (isEqual(VM, refVM)) {
        std::cout << "VM is equal to refVM" << std::endl;
        std::cout << "VM: " << VM.rotation() << std::endl;
        std::cout << "refVM: " << refVM.rotation() << std::endl;
    } else {
        std::cout << "VM is not equal to refVM" << std::endl;
        std::cout << "VM: " << VM.rotation() << std::endl;
        std::cout << "refVM: " << refVM.rotation() << std::endl;
    }
    return 0;
}
