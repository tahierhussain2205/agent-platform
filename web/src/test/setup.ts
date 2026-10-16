import '@testing-library/jest-dom/vitest'

// jsdom has no <dialog> support.
HTMLDialogElement.prototype.showModal = function () {
  this.setAttribute('open', '')
}
HTMLDialogElement.prototype.close = function () {
  this.removeAttribute('open')
}
